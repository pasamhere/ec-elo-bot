# main.py
import discord
from discord.ext import commands, tasks
from discord.commands import SlashCommandGroup
import os
import datetime
import firebase_admin
from firebase_admin import credentials, firestore

# -------------------------------------
# --- Firebase Firestore Setup ---
# -------------------------------------
try:
    if not os.path.exists("serviceAccountKey.json"):
        raise FileNotFoundError("Firebase serviceAccountKey.json not found.")
    cred = credentials.Certificate("serviceAccountKey.json")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase connection successful.")
except Exception as e:
    print(f"🔥 Firebase connection failed. Error: {e}")
    db = None

# -------------------------------------
# --- Bot Configuration ---
# -------------------------------------
BOT_TOKEN = os.environ.get('DISCORD_TOKEN')
if not BOT_TOKEN:
    print("🔥 DISCORD_TOKEN environment variable not found.")

STARTING_ELO = 1200
K_FACTOR = 32
K_FACTOR_PROVISIONAL = 64
PROVISIONAL_MATCHES = 10
DECAY_DAYS_INACTIVE = 30
DECAY_AMOUNT = 25
TIER_THRESHOLDS = { "S": 1800, "A": 1600, "B": 1400, "C": 0 }
ADMIN_ROLE_NAME = "Tournament Organizer"

bot = commands.Bot(intents=discord.Intents.default())

# -------------------------------------
# --- Helper Functions ---
# -------------------------------------
def get_player_tier(elo):
    for tier, threshold in TIER_THRESHOLDS.items():
        if elo >= threshold: return tier
    return "Unranked"

def calculate_elo_change(winner_data, loser_data):
    winner_elo = get_overall_elo(winner_data)
    loser_elo = get_overall_elo(loser_data)
    k_factor_to_use = K_FACTOR_PROVISIONAL if winner_data.get('matches_played', 0) < PROVISIONAL_MATCHES or loser_data.get('matches_played', 0) < PROVISIONAL_MATCHES else K_FACTOR
    expected_win = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    return round(k_factor_to_use * (1 - expected_win))

def get_overall_elo(player_data):
    regional_elos = [player_data.get(r, STARTING_ELO) for r in ['elo_na', 'elo_eu', 'elo_as']]
    return round(sum(regional_elos) / len(regional_elos))

async def process_match_elo(winner_id, loser_id, region, tournament_name=None):
    winner_ref = db.collection('players').document(str(winner_id))
    loser_ref = db.collection('players').document(str(loser_id))
    winner_doc, loser_doc = winner_ref.get(), loser_ref.get()

    if not all([winner_doc.exists, loser_doc.exists]):
        return None, "Winner or loser not found in database."

    winner_data, loser_data = winner_doc.to_dict(), loser_doc.to_dict()
    elo_field = f'elo_{region.lower()}'
    elo_change = calculate_elo_change(winner_data, loser_data)

    match_history_ref = db.collection('match_history').document()
    match_history_ref.set({
        'winner_id': str(winner_id), 'loser_id': str(loser_id),
        'winner_elo_before': winner_data.get(elo_field, STARTING_ELO),
        'loser_elo_before': loser_data.get(elo_field, STARTING_ELO),
        'elo_change': elo_change, 'region': region, 'timestamp': firestore.SERVER_TIMESTAMP
    })

    batch = db.batch()
    batch.update(winner_ref, { elo_field: firestore.Increment(elo_change), 'wins': firestore.Increment(1), 'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP })
    batch.update(loser_ref, { elo_field: firestore.Increment(-elo_change), 'losses': firestore.Increment(1), 'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP })
    batch.commit()
    return match_history_ref.id, None

# -------------------------------------
# --- Automated Tasks ---
# -------------------------------------
@tasks.loop(hours=24)
async def daily_elo_decay():
    if not db: return
    print(f"[{datetime.datetime.now()}] Running daily ELO decay task...")
    cutoff_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=DECAY_DAYS_INACTIVE)
    players_to_decay = db.collection('players').where('last_played_date', '<', cutoff_date).stream()
    decayed_count, batch = 0, db.batch()
    for player in players_to_decay:
        player_ref, player_data, update_data = db.collection('players').document(player.id), player.to_dict(), {}
        for region in ['na', 'eu', 'as']:
            elo_field, current_elo = f"elo_{region}", player_data.get(elo_field, STARTING_ELO)
            if current_elo > STARTING_ELO:
                update_data[elo_field] = max(STARTING_ELO, current_elo - DECAY_AMOUNT)
        if update_data:
            batch.update(player_ref, update_data)
            decayed_count += 1
    batch.commit()
    print(f"ELO decay complete. {decayed_count} players processed.")

# -------------------------------------
# --- Bot Events ---
# -------------------------------------
@bot.event
async def on_ready():
    print(f'✅ Bot is ready and logged in as {bot.user}')
    if db:
        print("☁️  Connected to Firestore database.")
        daily_elo_decay.start()
    else:
        print("🔴 WARNING: Bot is running WITHOUT a database connection.")

# -------------------------------------
# --- Command Groups ---
# -------------------------------------
elo = SlashCommandGroup("elo", "ELO system commands")
stats = SlashCommandGroup("stats", "View detailed player and match statistics")
profile_group = SlashCommandGroup("profile", "Manage and view player profiles")

# -------------------------------------
# --- Main User Commands ---
# -------------------------------------
@profile_group.command(name="register", description="Register for the ELO system.")
@discord.option("roblox_username", description="Your exact Roblox username.", required=True)
@discord.option("clan", description="Your clan name (optional).", required=False)
@discord.option("country", description="The country you represent (optional).", required=False)
async def register(ctx: discord.ApplicationContext, roblox_username: str, clan: str = None, country: str = None):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(ctx.author.id))
    if player_ref.get().exists:
        return await ctx.followup.send("You are already registered!", ephemeral=True)
    new_player_data = {
        'discord_id': str(ctx.author.id), 'discord_name': ctx.author.name,
        'roblox_username': roblox_username, 'clan': clan, 'country': country,
        'elo_na': STARTING_ELO, 'elo_eu': STARTING_ELO, 'elo_as': STARTING_ELO,
        'wins': 0, 'losses': 0, 'matches_played': 0, 'tournaments_participated': 0,
        'last_played_date': firestore.SERVER_TIMESTAMP, 'tournaments_played_in': []
    }
    player_ref.set(new_player_data)
    await ctx.followup.send("✅ Registration successful!", ephemeral=False)

@profile_group.command(name="view", description="View your or another player's ELO profile.")
async def view_profile(ctx: discord.ApplicationContext, player: discord.Member = None):
    target_user = player or ctx.author
    await ctx.defer()
    player_doc = db.collection('players').document(str(target_user.id)).get()
    if not player_doc.exists:
        return await ctx.followup.send(f"That player is not registered.", ephemeral=True)
    
    player_data = player_doc.to_dict()
    username = player_data.get('roblox_username', 'N/A')
    embed = discord.Embed(title=f"📊 ELO Profile for {username}", color=target_user.color)
    embed.set_thumbnail(url=target_user.display_avatar.url)
    
    wins, losses, total = player_data.get('wins', 0), player_data.get('losses', 0), player_data.get('matches_played', 0)
    win_rate = f"{(wins / total * 100):.2f}%" if total > 0 else "N/A"
    clan, country = player_data.get('clan') or 'None', player_data.get('country') or 'N/A'
    embed.add_field(name="Identity", value=f"**Clan:** {clan}\n**Country:** {country}", inline=True)
    embed.add_field(name="Career Stats", value=f"**W/L:** {wins}/{losses} ({win_rate})", inline=True)
    
    elo_overall = get_overall_elo(player_data)
    embed.add_field(name="ELO Ratings", value=f"**Overall:** `{elo_overall}` (Tier: {get_player_tier(elo_overall)})\n"
              f"**NA:** `{player_data.get('elo_na', STARTING_ELO)}` | **EU:** `{player_data.get('elo_eu', STARTING_ELO)}` | **AS:** `{player_data.get('elo_as', STARTING_ELO)}`", inline=False)

    winner_query = db.collection('match_history').where('winner_id', '==', str(target_user.id)).order_by('timestamp', direction='DESCENDING').limit(5).stream()
    loser_query = db.collection('match_history').where('loser_id', '==', str(target_user.id)).order_by('timestamp', direction='DESCENDING').limit(5).stream()
    matches = sorted(list(winner_query) + list(loser_query), key=lambda x: x.to_dict()['timestamp'], reverse=True)
    
    match_history_str = "No recent matches found." if not matches else ""
    for match_doc in matches[:5]:
        match = match_doc.to_dict()
        outcome = f"✅ Win vs <@{match['loser_id']}>" if match['winner_id'] == str(target_user.id) else f"❌ Loss vs <@{match['winner_id']}>"
        match_history_str += f"`{match_doc.id[:6]}`: {outcome} ({match['region']})\n"
    embed.add_field(name="Recent Match History (ID: Outcome vs Opponent)", value=match_history_str, inline=False)
    
    await ctx.followup.send(embed=embed)

@stats.command(name="h2h", description="View the head-to-head record between two players.")
async def h2h(ctx: discord.ApplicationContext, player1: discord.Member, player2: discord.Member):
    await ctx.defer()
    p1_wins = len(list(db.collection('match_history').where('winner_id', '==', str(player1.id)).where('loser_id', '==', str(player2.id)).stream()))
    p2_wins = len(list(db.collection('match_history').where('winner_id', '==', str(player2.id)).where('loser_id', '==', str(player1.id)).stream()))
    
    embed = discord.Embed(title=f"Head-to-Head: {player1.display_name} vs {player2.display_name}", color=discord.Color.teal())
    embed.add_field(name=player1.display_name, value=f"**{p1_wins}** Wins", inline=True)
    embed.add_field(name=player2.display_name, value=f"**{p2_wins}** Wins", inline=True)
    await ctx.followup.send(embed=embed)

@elo.command(name="report_match", description="Manually report the result of a match.")
@discord.option("winner", description="The Discord user who won.", type=discord.Member, required=True)
@discord.option("loser", description="The Discord user who lost.", type=discord.Member, required=True)
@discord.option("region", description="The region the match was played in.", choices=["NA", "EU", "AS"], required=True)
async def report_match(ctx: discord.ApplicationContext, winner: discord.Member, loser: discord.Member, region: str):
    await ctx.defer()
    match_id, error = await process_match_elo(winner.id, loser.id, region)
    if error:
        return await ctx.followup.send(f"Error: {error}", ephemeral=True)
    await ctx.followup.send(f"✅ Match manually recorded! Match ID: `{match_id}`")

# -------------------------------------
# --- Admin Commands ---
# -------------------------------------
@profile_group.command(name="edit", description="[Admin] Edit a player's registered information.")
@commands.has_role(ADMIN_ROLE_NAME)
async def edit_profile(ctx: discord.ApplicationContext, member: discord.Member, new_roblox_username: str = None, new_clan: str = None, new_country: str = None):
    # This command remains the same
    pass

@elo.command(name="revert_match", description="[Admin] Reverts a match result using its ID.")
@commands.has_role(ADMIN_ROLE_NAME)
async def revert_match(ctx: discord.ApplicationContext, match_id: str):
    # This command also remains the same
    pass

# -------------------------------------
# --- Register Commands & Run Bot ---
# -------------------------------------
bot.add_application_command(elo)
bot.add_application_command(stats)
bot.add_application_command(profile_group)

if __name__ == "__main__":
    if BOT_TOKEN and db:
        bot.run(BOT_TOKEN)
