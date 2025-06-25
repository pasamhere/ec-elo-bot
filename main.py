# main.py
import discord
from discord.ext import commands
from discord.commands import SlashCommandGroup
import os
import firebase_admin
from firebase_admin import credentials, firestore

# -------------------------------------
# --- Firebase Firestore Setup ---
# -------------------------------------

# This script expects the `serviceAccountKey.json` file to be in the same directory.
try:
    if not os.path.exists("serviceAccountKey.json"):
        raise FileNotFoundError("Firebase serviceAccountKey.json not found.")
        
    cred = credentials.Certificate("serviceAccountKey.json")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase connection successful.")
except Exception as e:
    print(f"🔥 Firebase connection failed. Error: {e}")
    db = None

# -------------------------------------
# --- Bot Configuration ---
# -------------------------------------

# The token is read from an environment variable for security.
# On DigitalOcean, we will set this variable when we run the PM2 process manager.
BOT_TOKEN = os.environ.get('DISCORD_TOKEN')
if not BOT_TOKEN:
    print("🔥 DISCORD_TOKEN environment variable not found.")
    # We don't exit here, to allow the script to be testable without a token in some cases.

# --- ELO & Tier Configuration ---
STARTING_ELO = 1200
K_FACTOR = 32
TIER_THRESHOLDS = {
    "S": 1800, "A": 1600, "B": 1400, "C": 0
}
ADMIN_ROLE_NAME = "Tournament Organizer" # The exact, case-sensitive name of the admin role


bot = commands.Bot(intents=discord.Intents.default())

# -------------------------------------
# --- Helper Functions ---
# -------------------------------------

def get_player_tier(elo):
    """Returns the tier (S, A, B, C) based on ELO score."""
    for tier, threshold in TIER_THRESHOLDS.items():
        if elo >= threshold:
            return tier
    return "Unranked"

def calculate_elo_change(winner_elo, loser_elo):
    """Calculates the ELO points to be exchanged after a match."""
    expected_win = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    change = K_FACTOR * (1 - expected_win)
    return round(change)

def get_overall_elo(player_data):
    """Calculates a player's overall ELO as an average of their regional scores."""
    regional_elos = [
        player_data.get('elo_na', STARTING_ELO),
        player_data.get('elo_eu', STARTING_ELO),
        player_data.get('elo_as', STARTING_ELO)
    ]
    return round(sum(regional_elos) / len(regional_elos))

# -------------------------------------
# --- Bot Events ---
# -------------------------------------

@bot.event
async def on_ready():
    """Called when the bot successfully connects to Discord."""
    print(f'✅ Bot is ready and logged in as {bot.user}')
    if not db:
        print("🔴 WARNING: Bot is running WITHOUT a database connection.")
    else:
        print("☁️  Connected to Firestore database.")

# -------------------------------------
# --- User Slash Commands ---
# -------------------------------------

# Create a command group for better organization in Discord's UI
elo = SlashCommandGroup("elo", "Commands for the Empire Clash ELO system")

@elo.command(name="register", description="Register for the ELO leaderboard.")
@discord.option("roblox_username", description="Your exact Roblox username.", required=True)
async def register(ctx: discord.ApplicationContext, roblox_username: str):
    if not db:
        await ctx.interaction.response.send_message("Database is not connected. Contact an admin.", ephemeral=True)
        return
    
    # Defer the response to give the bot time to talk to the database
    await ctx.defer(ephemeral=True)

    player_ref = db.collection('players').document(str(ctx.author.id))
    if player_ref.get().exists:
        await ctx.followup.send("You are already registered!", ephemeral=True)
        return

    # Create the data for a new player
    new_player_data = {
        'discord_id': str(ctx.author.id),
        'discord_name': ctx.author.name,
        'roblox_username': roblox_username,
        'elo_na': STARTING_ELO,
        'elo_eu': STARTING_ELO,
        'elo_as': STARTING_ELO,
        'wins': 0,
        'losses': 0,
        'matches_played': 0,
        'tournaments_participated': 0,
        'last_played_date': firestore.SERVER_TIMESTAMP
    }
    # Save the new player data to Firestore
    player_ref.set(new_player_data)

    embed = discord.Embed(
        title="✅ Registration Successful!",
        description=f"Welcome, **{roblox_username}**! You've been added to the leaderboards with `{STARTING_ELO}` ELO.",
        color=discord.Color.green()
    )
    # Send the confirmation message (ephemeral=False makes it visible to everyone)
    await ctx.followup.send(embed=embed, ephemeral=False)

@elo.command(name="report_match", description="Report the result of a tournament match.")
@discord.option("winner", description="The Discord user who won.", type=discord.Member, required=True)
@discord.option("loser", description="The Discord user who lost.", type=discord.Member, required=True)
@discord.option("region", description="The region the match was played in.", choices=["NA", "EU", "AS"], required=True)
async def report_match(ctx: discord.ApplicationContext, winner: discord.Member, loser: discord.Member, region: str):
    if not db:
        await ctx.interaction.response.send_message("Database is not connected.", ephemeral=True)
        return

    await ctx.defer()
    if winner.id == loser.id:
        await ctx.followup.send("A player cannot play against themselves.", ephemeral=True)
        return

    # Get references to the player documents in Firestore
    winner_ref = db.collection('players').document(str(winner.id))
    loser_ref = db.collection('players').document(str(loser.id))
    winner_doc = winner_ref.get()
    loser_doc = loser_ref.get()

    if not all([winner_doc.exists, loser_doc.exists]):
        await ctx.followup.send("Both players must be registered with `/elo register`.", ephemeral=True)
        return

    winner_data, loser_data = winner_doc.to_dict(), loser_doc.to_dict()
    
    # Determine which ELO field to update based on the region
    elo_field = f'elo_{region.lower()}'
    winner_elo, loser_elo = winner_data.get(elo_field, STARTING_ELO), loser_data.get(elo_field, STARTING_ELO)

    # Calculate the ELO change
    elo_change = calculate_elo_change(winner_elo, loser_elo)
    new_winner_elo, new_loser_elo = winner_elo + elo_change, loser_elo - elo_change

    # Use a batch to perform all database writes at once for safety
    batch = db.batch()
    batch.update(winner_ref, {
        elo_field: new_winner_elo, 'wins': firestore.Increment(1),
        'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP
    })
    batch.update(loser_ref, {
        elo_field: new_loser_elo, 'losses': firestore.Increment(1),
        'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP
    })
    batch.commit()

    # Create and send the confirmation embed
    embed = discord.Embed(title="⚔️ Match Result Recorded!", description=f"**Region:** {region}", color=discord.Color.blue())
    embed.add_field(name=f"🏆 Winner: {winner_data['roblox_username']}", value=f"`{winner_elo}` -> `{new_winner_elo}` **(+{elo_change})**", inline=True)
    embed.add_field(name=f"💔 Loser: {loser_data['roblox_username']}", value=f"`{loser_elo}` -> `{new_loser_elo}` **(-{elo_change})**", inline=True)
    embed.set_footer(text=f"Reported by: {ctx.author.name}")
    await ctx.followup.send(embed=embed)

@elo.command(name="leaderboard", description="View the ELO leaderboard.")
@discord.option("region", description="The region to view.", choices=["Overall", "NA", "EU", "AS"], required=True)
async def leaderboard(ctx: discord.ApplicationContext, region: str):
    if not db:
        await ctx.interaction.response.send_message("Database is not connected.", ephemeral=True)
        return
    await ctx.defer()

    # Fetch all player documents from the collection
    all_players = [p.to_dict() for p in db.collection('players').stream()]

    # Define the function used for sorting based on the selected region
    if region == "Overall":
        sort_key_func = lambda p: get_overall_elo(p)
    else:
        elo_field = f'elo_{region.lower()}'
        sort_key_func = lambda p: p.get(elo_field, STARTING_ELO)
    
    sorted_players = sorted(all_players, key=sort_key_func, reverse=True)
    
    embed = discord.Embed(title=f"🏆 Empire Clash Leaderboard - {region}", color=discord.Color.gold())
    if not sorted_players:
        embed.description = "The leaderboard is empty! Register with `/elo register`."
        await ctx.followup.send(embed=embed)
        return

    # Build the leaderboard string
    medals = ["🥇", "🥈", "🥉"]
    lb_string = ""
    for i, player in enumerate(sorted_players[:10]): # Show top 10
        rank_display = medals[i] if i < 3 else f"`#{i+1: <2}`"
        elo_score = get_overall_elo(player) if region == "Overall" else player.get(f'elo_{region.lower()}', STARTING_ELO)
        lb_string += f"{rank_display} **{player.get('roblox_username', 'Unknown')}** - `{elo_score}` ELO (Tier: {get_player_tier(elo_score)})\n"
    
    embed.add_field(name="Top 10 Rankings", value=lb_string, inline=False)
    await ctx.followup.send(embed=embed)

@elo.command(name="profile", description="View your or another player's ELO profile.")
@discord.option("player", description="Player to see (optional).", type=discord.Member, required=False)
async def profile(ctx: discord.ApplicationContext, player: discord.Member = None):
    if not db:
        await ctx.interaction.response.send_message("Database is not connected.", ephemeral=True)
        return
    
    # If no player is specified, target the user who ran the command
    target_user = player or ctx.author
    await ctx.defer()
    
    player_doc = db.collection('players').document(str(target_user.id)).get()
    if not player_doc.exists:
        await ctx.followup.send(f"{'They are' if player else 'You are'} not registered.", ephemeral=True)
        return

    player_data = player_doc.to_dict()
    username = player_data.get('roblox_username', 'N/A')
    elo_overall = get_overall_elo(player_data)
    wins, losses, total = player_data.get('wins', 0), player_data.get('losses', 0), player_data.get('matches_played', 0)
    win_rate = f"{(wins / total * 100):.2f}%" if total > 0 else "N/A"

    embed = discord.Embed(title=f"📊 ELO Profile for {username}", color=target_user.color)
    embed.set_thumbnail(url=target_user.display_avatar.url)
    embed.add_field(name="Overall Stats", value=f"**W/L:** `{wins}`/`{losses}`\n**Win Rate:** `{win_rate}`", inline=False)
    embed.add_field(name="Regional ELO", value=f"**Overall:** `{elo_overall}` (Tier: {get_player_tier(elo_overall)})\n"
              f"**NA:** `{player_data.get('elo_na', STARTING_ELO)}` | **EU:** `{player_data.get('elo_eu', STARTING_ELO)}` | **AS:** `{player_data.get('elo_as', STARTING_ELO)}`", inline=False)
    await ctx.followup.send(embed=embed)

# -------------------------------------
# --- Admin Commands ---
# -------------------------------------
admin = elo.create_subgroup("admin", "Admin-only commands for managing the ELO system.")

@admin.command(name="set_elo", description="[Admin] Manually set a player's ELO.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("player", description="The player to modify.", type=discord.Member, required=True)
@discord.option("region", description="Region's ELO to set.", choices=["NA", "EU", "AS"], required=True)
@discord.option("elo_value", description="The new ELO value.", type=int, required=True)
async def set_elo(ctx: discord.ApplicationContext, player: discord.Member, region: str, elo_value: int):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(player.id))
    if not player_ref.get().exists:
        await ctx.followup.send("This player is not registered.", ephemeral=True)
        return
    player_ref.update({f'elo_{region.lower()}': elo_value})
    await ctx.followup.send(f"✅ Set **{player.display_name}**'s ELO for **{region}** to `{elo_value}`.", ephemeral=True)

@admin.command(name="delete_player", description="[Admin] Remove a player from the system.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("player", description="The player to delete.", type=discord.Member, required=True)
async def delete_player(ctx: discord.ApplicationContext, player: discord.Member):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(player.id))
    if not player_ref.get().exists:
        await ctx.followup.send("This player is not registered.", ephemeral=True)
        return
    player_ref.delete()
    await ctx.followup.send(f"🗑️ Deleted **{player.display_name}** from the ELO system.", ephemeral=True)

@set_elo.error
@delete_player.error
async def admin_command_error(ctx, error):
    """Handles errors for all admin commands."""
    if isinstance(error, commands.MissingRole):
        await ctx.interaction.response.send_message(f"You need the `{ADMIN_ROLE_NAME}` role for this command.", ephemeral=True)
    else:
        print(f"An admin command error occurred: {error}")
        await ctx.interaction.response.send_message("An unexpected error occurred.", ephemeral=True)

# -------------------------------------
# --- Run the Bot ---
# -------------------------------------
bot.add_application_command(elo)

if __name__ == "__main__":
    if BOT_TOKEN and db:
        bot.run(BOT_TOKEN)
    elif not BOT_TOKEN:
        print("🔴 Bot cannot start: DISCORD_TOKEN environment variable is missing.")
    elif not db:
        print("🔴 Bot cannot start: Database connection failed.")
