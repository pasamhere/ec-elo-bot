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
ADMIN_ROLE_NAME = "Tournament Organizer"
TIER_THRESHOLDS = { "S-Tier": 1800, "A-Tier": 1600, "B-Tier": 1400, "C-Tier": 0 }

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
    k_factor = K_FACTOR_PROVISIONAL if winner_data.get('matches_played', 0) < PROVISIONAL_MATCHES or loser_data.get('matches_played', 0) < PROVISIONAL_MATCHES else K_FACTOR
    expected_win = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    return round(k_factor * (1 - expected_win))

def get_overall_elo(player_data):
    return round(sum([player_data.get(r, STARTING_ELO) for r in ['elo_na', 'elo_eu', 'elo_as']]) / 3)

async def process_match_elo(guild, winner_id, loser_id, region):
    winner_ref = db.collection('players').document(str(winner_id))
    loser_ref = db.collection('players').document(str(loser_id))
    winner_doc, loser_doc = winner_ref.get(), loser_ref.get()

    if not all([winner_doc.exists, loser_doc.exists]):
        return None, "Winner or loser not found in database."

    winner_data, loser_data = winner_doc.to_dict(), loser_doc.to_dict()
    elo_field = f'elo_{region.lower()}'
    elo_change = calculate_elo_change(winner_data, loser_data)
    
    batch = db.batch()
    batch.update(winner_ref, { elo_field: firestore.Increment(elo_change), 'wins': firestore.Increment(1), 'matches_played': firestore.Increment(1) })
    batch.update(loser_ref, { elo_field: firestore.Increment(-elo_change), 'losses': firestore.Increment(1), 'matches_played': firestore.Increment(1) })
    batch.commit()
    
    match_history_ref = db.collection('match_history').document()
    match_history_ref.set({'winner_id': str(winner_id), 'loser_id': str(loser_id), 'elo_change': elo_change, 'region': region, 'timestamp': firestore.SERVER_TIMESTAMP})
    return match_history_ref.id, None

# -------------------------------------
# --- Bot Events ---
# -------------------------------------
@bot.event
async def on_ready():
    print(f'✅ Bot is ready and logged in as {bot.user}')
    if db: print("☁️  Connected to Firestore database.")
    else: print("🔴 WARNING: Bot is running WITHOUT a database connection.")

# -------------------------------------
# --- Command Groups ---
# -------------------------------------
elo = SlashCommandGroup("elo", "Core ELO system commands")
profile_group = SlashCommandGroup("profile", "Manage and view player profiles")

# -------------------------------------
# --- User Commands ---
# -------------------------------------
@profile_group.command(name="register", description="Register for the ELO system.")
@discord.option("roblox_username", description="Your exact Roblox username.", required=True)
async def register(ctx: discord.ApplicationContext, roblox_username: str):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(ctx.author.id))
    if player_ref.get().exists:
        return await ctx.followup.send("You are already registered!", ephemeral=True)
    new_player_data = {
        'discord_id': str(ctx.author.id), 'discord_name': ctx.author.name, 'roblox_username': roblox_username,
        'elo_na': STARTING_ELO, 'elo_eu': STARTING_ELO, 'elo_as': STARTING_ELO,
        'wins': 0, 'losses': 0, 'matches_played': 0
    }
    player_ref.set(new_player_data)
    await ctx.followup.send("✅ Registration successful!", ephemeral=False)

@profile_group.command(name="view", description="View your or another player's ELO profile.")
@discord.option("player", description="The player whose profile you want to see.", type=discord.Member, required=False)
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
    embed.add_field(name="Career Stats", value=f"**W/L:** {wins}/{losses} ({win_rate})", inline=False)
    
    elo_overall = get_overall_elo(player_data)
    embed.add_field(name="ELO Ratings", value=f"**Overall:** `{elo_overall}` (Tier: {get_player_tier(elo_overall)})\n"
              f"**NA:** `{player_data.get('elo_na', STARTING_ELO)}` | **EU:** `{player_data.get('elo_eu', STARTING_ELO)}` | **AS:** `{player_data.get('elo_as', STARTING_ELO)}`", inline=False)
    await ctx.followup.send(embed=embed)

@elo.command(name="report_match", description="Manually report the result of a match.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("winner", description="The Discord user who won.", type=discord.Member, required=True)
@discord.option("loser", description="The Discord user who lost.", type=discord.Member, required=True)
@discord.option("region", description="The region the match was played in.", choices=["NA", "EU", "AS"], required=True)
async def report_match(ctx: discord.ApplicationContext, winner: discord.Member, loser: discord.Member, region: str):
    await ctx.defer()
    match_id, error = await process_match_elo(ctx.guild, winner.id, loser.id, region)
    if error:
        return await ctx.followup.send(f"Error: {error}", ephemeral=True)
    await ctx.followup.send(f"✅ Match manually recorded! Keep the ID `{match_id}` to revert if needed.")

@elo.command(name="leaderboard", description="View the ELO leaderboard.")
@discord.option("region", description="The region to view.", choices=["Overall", "NA", "EU", "AS"], required=True)
async def leaderboard(ctx: discord.ApplicationContext, region: str):
    await ctx.defer()
    all_players = [p.to_dict() for p in db.collection('players').stream()]
    sort_key_func = get_overall_elo if region == "Overall" else lambda p: p.get(f'elo_{region.lower()}', STARTING_ELO)
    sorted_players = sorted(all_players, key=sort_key_func, reverse=True)
    embed = discord.Embed(title=f"🏆 Empire Clash Leaderboard - {region}", color=discord.Color.gold())
    if not sorted_players:
        embed.description = "The leaderboard is empty!"
        return await ctx.followup.send(embed=embed)
    medals, lb_string = ["🥇", "🥈", "🥉"], ""
    for i, player in enumerate(sorted_players[:10]):
        rank_display = medals[i] if i < 3 else f"`#{i+1: <2}`"
        elo_score = get_overall_elo(player) if region == "Overall" else player.get(f'elo_{region.lower()}', STARTING_ELO)
        lb_string += f"{rank_display} **{player.get('roblox_username', 'Unknown')}** - `{elo_score}` ELO ({get_player_tier(elo_score)})\n"
    embed.add_field(name="Top 10 Rankings", value=lb_string or "No players found.", inline=False)
    await ctx.followup.send(embed=embed)

# -------------------------------------
# --- Admin Commands ---
# -------------------------------------
@profile_group.command(name="edit", description="Edit a player's registered information.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("member", description="The player to edit.", type=discord.Member, required=True)
@discord.option("new_roblox_username", description="The player's corrected Roblox username.", required=True)
async def edit_profile(ctx: discord.ApplicationContext, member: discord.Member, new_roblox_username: str):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(member.id))
    if not player_ref.get().exists: return await ctx.followup.send("Player not found.", ephemeral=True)
    player_ref.update({'roblox_username': new_roblox_username})
    await ctx.followup.send(f"✅ Successfully updated username for {member.display_name}.", ephemeral=True)

@elo.command(name="set", description="Manually set a player's ELO in a specific region.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("player", description="The player to modify.", type=discord.Member, required=True)
@discord.option("region", description="The region to set ELO for.", choices=["NA", "EU", "AS"], required=True)
@discord.option("value", description="The new ELO value.", type=int, required=True)
async def set_elo(ctx: discord.ApplicationContext, player: discord.Member, region: str, value: int):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(player.id))
    if not player_ref.get().exists: return await ctx.followup.send("Player not found.", ephemeral=True)
    elo_field = f'elo_{region.lower()}'
    player_ref.update({elo_field: value})
    await ctx.followup.send(f"✅ Set {player.display_name}'s {region} ELO to {value}.", ephemeral=True)

# -------------------------------------
# --- Register Commands & Run Bot ---
# -------------------------------------
bot.add_application_command(elo)
bot.add_application_command(profile_group)

if __name__ == "__main__":
    if BOT_TOKEN and db:
        bot.run(BOT_TOKEN)
