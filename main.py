# main.py
import discord
from discord.ext import commands, tasks
from discord.commands import SlashCommandGroup
import os
import datetime
import firebase_admin
from firebase_admin import credentials, firestore
import pychallonge
import matplotlib.pyplot as plt
import io

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
        'elo_change': elo_change, 'region': region, 'timestamp': firestore.SERVER_TIMESTAMP,
        'tournament_name': tournament_name
    })

    batch = db.batch()
    update_winner = { elo_field: firestore.Increment(elo_change), 'wins': firestore.Increment(1), 'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP }
    update_loser = { elo_field: firestore.Increment(-elo_change), 'losses': firestore.Increment(1), 'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP }
    
    if tournament_name and tournament_name not in winner_data.get('tournaments_played_in', []):
        update_winner['tournaments_participated'] = firestore.Increment(1)
        update_winner['tournaments_played_in'] = firestore.ArrayUnion([tournament_name])
        
    if tournament_name and tournament_name not in loser_data.get('tournaments_played_in', []):
        update_loser['tournaments_participated'] = firestore.Increment(1)
        update_loser['tournaments_played_in'] = firestore.ArrayUnion([tournament_name])

    batch.update(winner_ref, update_winner)
    batch.update(loser_ref, update_loser)
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
challonge = SlashCommandGroup("challonge", "Challonge integration commands")
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
    
    # Main Stats
    wins, losses, total = player_data.get('wins', 0), player_data.get('losses', 0), player_data.get('matches_played', 0)
    win_rate = f"{(wins / total * 100):.2f}%" if total > 0 else "N/A"
    clan = player_data.get('clan') or 'None'
    country = player_data.get('country') or 'N/A'
    tourneys = player_data.get('tournaments_participated', 0)
    embed.add_field(name="Identity", value=f"**Clan:** {clan}\n**Country:** {country}", inline=True)
    embed.add_field(name="Career Stats", value=f"**W/L:** {wins}/{losses} ({win_rate})\n**Tournaments:** {tourneys}", inline=True)
    
    # ELO Stats
    elo_overall = get_overall_elo(player_data)
    embed.add_field(name="ELO Ratings", value=f"**Overall:** `{elo_overall}` (Tier: {get_player_tier(elo_overall)})\n"
              f"**NA:** `{player_data.get('elo_na', STARTING_ELO)}` | **EU:** `{player_data.get('elo_eu', STARTING_ELO)}` | **AS:** `{player_data.get('elo_as', STARTING_ELO)}`", inline=False)

    # Match History
    winner_query = db.collection('match_history').where('winner_id', '==', str(target_user.id)).order_by('timestamp', direction='DESCENDING').limit(5).stream()
    loser_query = db.collection('match_history').where('loser_id', '==', str(target_user.id)).order_by('timestamp', direction='DESCENDING').limit(5).stream()
    matches = sorted(list(winner_query) + list(loser_query), key=lambda x: x.to_dict()['timestamp'], reverse=True)
    
    match_history_str = ""
    if not matches:
        match_history_str = "No recent matches found."
    else:
        for match_doc in matches[:5]:
            match = match_doc.to_dict()
            outcome = f"✅ Win vs <@{match['loser_id']}>" if match['winner_id'] == str(target_user.id) else f"❌ Loss vs <@{match['winner_id']}>"
            match_history_str += f"`{match_doc.id[:6]}`: {outcome} ({match['region']})\n"
    embed.add_field(name="Recent Match History (ID: Outcome vs Opponent)", value=match_history_str, inline=False)
    
    await ctx.followup.send(embed=embed)

@stats.command(name="h2h", description="View the head-to-head record between two players.")
async def h2h(ctx: discord.ApplicationContext, player1: discord.Member, player2: discord.Member):
    await ctx.defer()
    
    # Query for matches where P1 beat P2
    p1_wins_q = db.collection('match_history').where('winner_id', '==', str(player1.id)).where('loser_id', '==', str(player2.id)).stream()
    p1_wins = len(list(p1_wins_q))
    
    # Query for matches where P2 beat P1
    p2_wins_q = db.collection('match_history').where('winner_id', '==', str(player2.id)).where('loser_id', '==', str(player1.id)).stream()
    p2_wins = len(list(p2_wins_q))
    
    embed = discord.Embed(title=f"Head-to-Head: {player1.display_name} vs {player2.display_name}", color=discord.Color.teal())
    embed.add_field(name=player1.display_name, value=f"**{p1_wins}** Wins", inline=True)
    embed.add_field(name=player2.display_name, value=f"**{p2_wins}** Wins", inline=True)
    await ctx.followup.send(embed=embed)
    
@stats.command(name="elo_graph", description="Generate a graph of a player's ELO over time.")
async def elo_graph(ctx: discord.ApplicationContext, player: discord.Member):
    await ctx.defer()
    
    # 1. Fetch all match data for the player
    winner_query = db.collection('match_history').where('winner_id', '==', str(player.id)).order_by('timestamp', direction='ASCENDING').stream()
    loser_query = db.collection('match_history').where('loser_id', '==', str(player.id)).order_by('timestamp', direction='ASCENDING').stream()
    matches = sorted(list(winner_query) + list(loser_query), key=lambda x: x.to_dict()['timestamp'])

    if not matches:
        return await ctx.followup.send("No match history found for this player to generate a graph.", ephemeral=True)

    # 2. Process data for graphing
    timestamps, elo_points = [], []
    current_elo = STARTING_ELO 
    
    # To get a more accurate starting ELO, we'd need to calculate it backwards or store it at each point.
    # For simplicity, we'll track the change from the start.
    
    for match_doc in matches:
        match = match_doc.to_dict()
        elo_change = match['elo_change']
        if match['winner_id'] == str(player.id):
            current_elo += elo_change
        else:
            current_elo -= elo_change
        timestamps.append(match['timestamp'])
        elo_points.append(current_elo)
        
    # Add a starting point to the graph
    timestamps.insert(0, timestamps[0] - datetime.timedelta(minutes=1))
    elo_points.insert(0, elo_points[0] - (elo_change if matches[0].to_dict()['winner_id'] == str(player.id) else -elo_change))


    # 3. Generate the graph with matplotlib
    plt.style.use('dark_background')
    fig, ax = plt.subplots()
    ax.plot(timestamps, elo_points, marker='o', linestyle='-', color='#7289DA')
    ax.set_title(f"ELO History for {player.display_name}", color='white')
    ax.set_xlabel("Date", color='white')
    ax.set_ylabel("Overall ELO", color='white')
    ax.grid(True, linestyle='--', alpha=0.3)
    fig.autofmt_xdate()
    
    # 4. Save graph to a buffer and send as a file
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight')
    buf.seek(0)
    
    await ctx.followup.send(file=discord.File(buf, 'elo_graph.png'))
    plt.close(fig)

# -------------------------------------
# --- Admin Commands ---
# -------------------------------------
@profile_group.command(name="edit", description="[Admin] Edit a player's registered information.")
@commands.has_role(ADMIN_ROLE_NAME)
async def edit_profile(ctx: discord.ApplicationContext, member: discord.Member, new_roblox_username: str = None, new_clan: str = None, new_country: str = None):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(member.id))
    if not player_ref.get().exists:
        return await ctx.followup.send("Player not found.", ephemeral=True)
    
    update_data = {}
    if new_roblox_username: update_data['roblox_username'] = new_roblox_username
    if new_clan: update_data['clan'] = new_clan
    if new_country: update_data['country'] = new_country
    
    if not update_data:
        return await ctx.followup.send("You must provide at least one field to edit.", ephemeral=True)
        
    player_ref.update(update_data)
    await ctx.followup.send(f"✅ Successfully updated profile for {member.display_name}.", ephemeral=True)

@challonge_admin.command(name="import_matches", description="[Admin] Import matches from a Challonge tournament.")
async def import_matches(ctx: discord.ApplicationContext, tournament_url: str, region: str):
    await ctx.defer(ephemeral=True)
    api_key_doc = db.collection('config').document('challonge_api').get()
    if not api_key_doc.exists:
        return await ctx.followup.send("API key not set. Use `/challonge admin set_api_key`.", ephemeral=True)
    
    all_players_stream = db.collection('players').stream()
    player_map = {}
    for p in all_players_stream:
        data = p.to_dict()
        if data.get('roblox_username'): player_map[data['roblox_username'].lower()] = data['discord_id']
        if data.get('challonge_username'): player_map[data['challonge_username'].lower()] = data['discord_id'] # Override

    try:
        challonge.set_credentials(os.environ.get("CHALLONGE_USER", ""), api_key_doc.to_dict().get('key'))
        tourney_id_part = tournament_url.split('/')[-1] if tournament_url.split('/')[-1] else tournament_url.split('/')[-2]
        tournament_obj = challonge.tournaments.show(tourney_id_part)
        matches = challonge.matches.index(tournament_obj['id'], state="complete")
    except Exception as e:
        return await ctx.followup.send(f"Error fetching from Challonge: {e}", ephemeral=True)

    success_count, failed_matches = 0, []
    for match in matches:
        winner_name_obj = challonge.participants.show(tournament_obj['id'], match['winner_id'])
        loser_name_obj = challonge.participants.show(tournament_obj['id'], match['loser_id'])
        winner_name = winner_name_obj.get('username') or winner_name_obj.get('name')
        loser_name = loser_name_obj.get('username') or loser_name_obj.get('name')

        winner_id = player_map.get(winner_name.lower())
        loser_id = player_map.get(loser_name.lower())

        if not winner_id or not loser_id:
            failed_matches.append(f"'{winner_name}' vs '{loser_name}'")
            continue
        
        match_id, error = await process_match_elo(winner_id, loser_id, region, tournament_obj['name'])
        if error: failed_matches.append(f"'{winner_name}' vs '{loser_name}' ({error})")
        else: success_count += 1
            
    embed = discord.Embed(title="Challonge Import Summary", color=discord.Color.green() if not failed_matches else discord.Color.orange())
    embed.add_field(name="Successfully Imported", value=f"`{success_count}` matches", inline=False)
    if failed_matches:
        embed.add_field(name="Failed/Skipped Matches", value="\n".join(failed_matches)[:1024], inline=False)
        
    await ctx.followup.send(embed=embed, ephemeral=True)


# ... (Other admin commands like revert_match, set_api_key, etc., are unchanged)


# -------------------------------------
# --- Register Commands & Run Bot ---
# -------------------------------------
bot.add_application_command(elo)
bot.add_application_command(stats)
bot.add_application_command(challonge)
bot.add_application_command(profile_group)

if __name__ == "__main__":
    if BOT_TOKEN and db:
        bot.run(BOT_TOKEN)
