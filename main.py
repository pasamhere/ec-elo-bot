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
# --- Views (for buttons) ---
# -------------------------------------
class DeregisterView(discord.ui.View):
    def __init__(self, user_to_deregister: discord.Member):
        super().__init__(timeout=30)
        self.user_to_deregister = user_to_deregister
        self.confirmed = None

    @discord.ui.button(label="Yes, Deregister Me", style=discord.ButtonStyle.danger)
    async def confirm_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.user_to_deregister.id:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        self.confirmed = True
        self.stop()
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="No, Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.user_to_deregister.id:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
        self.confirmed = False
        self.stop()
        for child in self.children: child.disabled = True
        await interaction.response.edit_message(view=self)

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

async def process_match_elo(winner_id, loser_id, region):
    winner_ref = db.collection('players').document(str(winner_id))
    loser_ref = db.collection('players').document(str(loser_id))
    winner_doc, loser_doc = winner_ref.get(), loser_ref.get()

    if not all([winner_doc.exists, loser_doc.exists]):
        return None, "Winner or loser not found in database."

    winner_data, loser_data = winner_doc.to_dict(), loser_doc.to_dict()
    elo_field = f'elo_{region.lower()}'
    elo_change = calculate_elo_change(winner_data, loser_data)
    
    # Store match data first to get a unique ID
    match_history_ref = db.collection('match_history').document()
    match_history_ref.set({
        'winner_id': str(winner_id), 'loser_id': str(loser_id),
        'elo_change': elo_change, 'region': region, 'timestamp': firestore.SERVER_TIMESTAMP
    })

    # Update player stats
    batch = db.batch()
    batch.update(winner_ref, { elo_field: firestore.Increment(elo_change), 'wins': firestore.Increment(1), 'matches_played': firestore.Increment(1) })
    batch.update(loser_ref, { elo_field: firestore.Increment(-elo_change), 'losses': firestore.Increment(1), 'matches_played': firestore.Increment(1) })
    batch.commit()
    
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
    
    # --- NEW: Match History ---
    winner_query = db.collection('match_history').where('winner_id', '==', str(target_user.id)).order_by('timestamp', direction='DESCENDING').limit(5).stream()
    loser_query = db.collection('match_history').where('loser_id', '==', str(target_user.id)).order_by('timestamp', direction='DESCENDING').limit(5).stream()
    matches = sorted(list(winner_query) + list(loser_query), key=lambda x: x.to_dict()['timestamp'], reverse=True)
    
    match_history_str = "No recent matches found."
    if matches:
        match_history_str = ""
        for match_doc in matches[:5]:
            match = match_doc.to_dict()
            outcome = f"✅ Win vs <@{match['loser_id']}>" if match['winner_id'] == str(target_user.id) else f"❌ Loss vs <@{match['winner_id']}>"
            match_history_str += f"`{match_doc.id}`: {outcome} ({match['region']})\n"
    embed.add_field(name="Recent Match History (ID: Outcome vs Opponent)", value=match_history_str, inline=False)

    await ctx.followup.send(embed=embed)


@profile_group.command(name="deregister", description="Permanently remove yourself from the ELO system.")
async def deregister(ctx: discord.ApplicationContext):
    player_ref = db.collection('players').document(str(ctx.author.id))
    if not player_ref.get().exists:
        return await ctx.interaction.response.send_message("You are not registered.", ephemeral=True)

    view = DeregisterView(ctx.author)
    await ctx.interaction.response.send_message(
        "**Are you sure you want to deregister?** All your stats and ELO will be permanently deleted.",
        view=view, ephemeral=True
    )
    await view.wait()

    if view.confirmed:
        player_ref.delete()
        await ctx.followup.send("You have been successfully deregistered.", ephemeral=True)
    else:
        await ctx.followup.send("Deregistration cancelled.", ephemeral=True)


@elo.command(name="report_match", description="Manually report the result of a match.")
@commands.has_role(ADMIN_ROLE_NAME)
async def report_match(ctx: discord.ApplicationContext, winner: discord.Member, loser: discord.Member, region: str):
    await ctx.defer()
    match_id, error = await process_match_elo(ctx.guild, winner.id, loser.id, region)
    if error:
        return await ctx.followup.send(f"Error: {error}", ephemeral=True)
    await ctx.followup.send(f"✅ Match manually recorded! Match ID: `{match_id}`")


@elo.command(name="leaderboard", description="View the ELO leaderboard.")
@discord.option("region", description="The region to view.", choices=["Overall", "NA", "EU", "AS"], required=True)
async def leaderboard(ctx: discord.ApplicationContext, region: str):
    # ... (code is unchanged)
    pass

# -------------------------------------
# --- Admin Commands ---
# -------------------------------------
admin_profile = profile_group.create_subgroup("admin", "Admin-only profile commands.")

@admin_profile.command(name="deregister_member", description="Forcibly remove a player from the ELO system.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("member", description="The member to deregister.", type=discord.Member, required=True)
async def deregister_member(ctx: discord.ApplicationContext, member: discord.Member):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(member.id))
    if not player_ref.get().exists:
        return await ctx.followup.send(f"**{member.display_name}** is not registered.", ephemeral=True)
    player_ref.delete()
    await ctx.followup.send(f"🗑️ Successfully deregistered **{member.display_name}**.", ephemeral=True)

@admin_profile.command(name="edit", description="Edit a player's registered information.")
@commands.has_role(ADMIN_ROLE_NAME)
async def edit_profile(ctx: discord.ApplicationContext, member: discord.Member, new_roblox_username: str):
    # ... (code is unchanged)
    pass

@elo.command(name="set", description="Manually set a player's ELO in a specific region.")
@commands.has_role(ADMIN_ROLE_NAME)
async def set_elo(ctx: discord.ApplicationContext, player: discord.Member, region: str, value: int):
    # ... (code is unchanged)
    pass

@elo.command(name="revert_match", description="Reverts a match result using its ID.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("match_id", description="The full ID of the match from a player's profile.", required=True)
async def revert_match(ctx: discord.ApplicationContext, match_id: str):
    await ctx.defer(ephemeral=True)
    match_ref = db.collection('match_history').document(match_id)
    match_doc = match_ref.get()
    if not match_doc.exists: return await ctx.followup.send("Error: Match ID not found.", ephemeral=True)
    
    match_data = match_doc.to_dict()
    winner_ref = db.collection('players').document(match_data['winner_id'])
    loser_ref = db.collection('players').document(match_data['loser_id'])
    elo_field = f"elo_{match_data['region'].lower()}"
    elo_change = match_data['elo_change']

    batch = db.batch()
    batch.update(winner_ref, { elo_field: firestore.Increment(-elo_change), 'wins': firestore.Increment(-1), 'matches_played': firestore.Increment(-1) })
    batch.update(loser_ref, { elo_field: firestore.Increment(elo_change), 'losses': firestore.Increment(-1), 'matches_played': firestore.Increment(-1) })
    batch.delete(match_ref) # Delete the match record
    batch.commit()
    
    await ctx.followup.send(f"✅ Successfully reverted Match ID `{match_id}`.", ephemeral=True)

# -------------------------------------
# --- Register Commands & Run Bot ---
# -------------------------------------
bot.add_application_command(elo)
bot.add_application_command(profile_group)

if __name__ == "__main__":
    if BOT_TOKEN and db:
        bot.run(BOT_TOKEN)
