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

BOT_TOKEN = os.environ.get('DISCORD_TOKEN')
if not BOT_TOKEN:
    print("🔥 DISCORD_TOKEN environment variable not found.")

STARTING_ELO = 1200
K_FACTOR = 32
TIER_THRESHOLDS = {
    "S": 1800, "A": 1600, "B": 1400, "C": 0
}
ADMIN_ROLE_NAME = "Tournament Organizer"


# --- Views for Buttons ---
class DeregisterView(discord.ui.View):
    def __init__(self, user_to_deregister: discord.Member):
        super().__init__(timeout=30)
        self.user_to_deregister = user_to_deregister
        self.confirmed = None

    @discord.ui.button(label="Yes, Deregister Me", style=discord.ButtonStyle.danger)
    async def confirm_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        # Ensure the person clicking the button is the one being deregistered
        if interaction.user.id != self.user_to_deregister.id:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return
            
        self.confirmed = True
        self.stop()
        # Disable the buttons after click
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)


    @discord.ui.button(label="No, Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        if interaction.user.id != self.user_to_deregister.id:
            await interaction.response.send_message("This is not for you!", ephemeral=True)
            return

        self.confirmed = False
        self.stop()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)


bot = commands.Bot(intents=discord.Intents.default())

# -------------------------------------
# --- Helper Functions ---
# -------------------------------------

def get_player_tier(elo):
    for tier, threshold in TIER_THRESHOLDS.items():
        if elo >= threshold:
            return tier
    return "Unranked"

def calculate_elo_change(winner_elo, loser_elo):
    expected_win = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    change = K_FACTOR * (1 - expected_win)
    return round(change)

def get_overall_elo(player_data):
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
    print(f'✅ Bot is ready and logged in as {bot.user}')
    if not db:
        print("🔴 WARNING: Bot is running WITHOUT a database connection.")
    else:
        print("☁️  Connected to Firestore database.")

# -------------------------------------
# --- Slash Command Groups ---
# -------------------------------------
elo = SlashCommandGroup("elo", "Commands for the Empire Clash ELO system")
tournament = SlashCommandGroup("tournament", "Commands for managing tournaments")

# -------------------------------------
# --- ELO Commands ---
# -------------------------------------

@elo.command(name="register", description="Register for the ELO leaderboard.")
@discord.option("roblox_username", description="Your exact Roblox username.", required=True)
async def register(ctx: discord.ApplicationContext, roblox_username: str):
    if not db:
        await ctx.interaction.response.send_message("Database is not connected. Contact an admin.", ephemeral=True)
        return
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(ctx.author.id))
    if player_ref.get().exists:
        await ctx.followup.send("You are already registered!", ephemeral=True)
        return
    new_player_data = {
        'discord_id': str(ctx.author.id), 'discord_name': ctx.author.name,
        'roblox_username': roblox_username, 'elo_na': STARTING_ELO,
        'elo_eu': STARTING_ELO, 'elo_as': STARTING_ELO, 'wins': 0, 'losses': 0,
        'matches_played': 0, 'tournaments_participated': 0,
        'last_played_date': firestore.SERVER_TIMESTAMP
    }
    player_ref.set(new_player_data)
    embed = discord.Embed(title="✅ Registration Successful!", description=f"Welcome, **{roblox_username}**!", color=discord.Color.green())
    await ctx.followup.send(embed=embed, ephemeral=False)

@elo.command(name="deregister", description="Deregister yourself from the ELO system. This is permanent!")
async def deregister(ctx: discord.ApplicationContext):
    if not db:
        return await ctx.interaction.response.send_message("Database not connected.", ephemeral=True)
    
    player_ref = db.collection('players').document(str(ctx.author.id))
    if not player_ref.get().exists:
        return await ctx.interaction.response.send_message("You are not registered.", ephemeral=True)

    view = DeregisterView(ctx.author)
    await ctx.interaction.response.send_message(
        "**Are you sure you want to deregister?** All your stats and ELO will be permanently deleted.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if view.confirmed is True:
        player_ref.delete()
        await ctx.followup.send("You have been successfully deregistered.", ephemeral=True)
    elif view.confirmed is False:
        await ctx.followup.send("Deregistration cancelled.", ephemeral=True)
    else: # Timeout
        await ctx.followup.send("Deregistration timed out.", ephemeral=True)


@elo.command(name="report_match", description="Report the result of a tournament match.")
@discord.option("winner", description="The Discord user who won.", type=discord.Member, required=True)
@discord.option("loser", description="The Discord user who lost.", type=discord.Member, required=True)
@discord.option("region", description="The region the match was played in.", choices=["NA", "EU", "AS"], required=True)
async def report_match(ctx: discord.ApplicationContext, winner: discord.Member, loser: discord.Member, region: str):
    if not db: return await ctx.interaction.response.send_message("Database not connected.", ephemeral=True)
    await ctx.defer()
    if winner.id == loser.id: return await ctx.followup.send("A player cannot play against themselves.", ephemeral=True)

    winner_ref = db.collection('players').document(str(winner.id))
    loser_ref = db.collection('players').document(str(loser.id))
    winner_doc, loser_doc = winner_ref.get(), loser_ref.get()

    if not all([winner_doc.exists, loser_doc.exists]):
        return await ctx.followup.send("Both players must be registered with `/elo register`.", ephemeral=True)

    winner_data, loser_data = winner_doc.to_dict(), loser_doc.to_dict()
    elo_field = f'elo_{region.lower()}'
    winner_elo, loser_elo = winner_data.get(elo_field, STARTING_ELO), loser_data.get(elo_field, STARTING_ELO)

    elo_change = calculate_elo_change(winner_elo, loser_elo)
    new_winner_elo, new_loser_elo = winner_elo + elo_change, loser_elo - elo_change

    batch = db.batch()
    batch.update(winner_ref, { elo_field: new_winner_elo, 'wins': firestore.Increment(1), 'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP })
    batch.update(loser_ref, { elo_field: new_loser_elo, 'losses': firestore.Increment(1), 'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP })
    batch.commit()

    embed = discord.Embed(title="⚔️ Match Result Recorded!", description=f"**Region:** {region}", color=discord.Color.blue())
    embed.add_field(name=f"🏆 Winner: {winner_data['roblox_username']}", value=f"`{winner_elo}` -> `{new_winner_elo}` **(+{elo_change})**", inline=True)
    embed.add_field(name=f"💔 Loser: {loser_data['roblox_username']}", value=f"`{loser_elo}` -> `{new_loser_elo}` **(-{elo_change})**", inline=True)
    embed.set_footer(text=f"Reported by: {ctx.author.name}")
    await ctx.followup.send(embed=embed)

@elo.command(name="leaderboard", description="View the ELO leaderboard.")
@discord.option("region", description="The region to view.", choices=["Overall", "NA", "EU", "AS"], required=True)
async def leaderboard(ctx: discord.ApplicationContext, region: str):
    if not db: return await ctx.interaction.response.send_message("Database not connected.", ephemeral=True)
    await ctx.defer()

    all_players = [p.to_dict() for p in db.collection('players').stream()]
    sort_key_func = get_overall_elo if region == "Overall" else lambda p: p.get(f'elo_{region.lower()}', STARTING_ELO)
    sorted_players = sorted(all_players, key=sort_key_func, reverse=True)
    
    embed = discord.Embed(title=f"🏆 Empire Clash Leaderboard - {region}", color=discord.Color.gold())
    if not sorted_players:
        embed.description = "The leaderboard is empty!"
        return await ctx.followup.send(embed=embed)

    medals = ["🥇", "🥈", "🥉"]
    lb_string = ""
    for i, player in enumerate(sorted_players[:10]):
        rank_display = medals[i] if i < 3 else f"`#{i+1: <2}`"
        elo_score = get_overall_elo(player) if region == "Overall" else player.get(f'elo_{region.lower()}', STARTING_ELO)
        lb_string += f"{rank_display} **{player.get('roblox_username', 'Unknown')}** - `{elo_score}` ELO (Tier: {get_player_tier(elo_score)})\n"
    
    embed.add_field(name="Top 10 Rankings", value=lb_string or "No players found.", inline=False)
    await ctx.followup.send(embed=embed)

@elo.command(name="profile", description="View your or another player's ELO profile.")
@discord.option("player", description="Player to see (optional).", type=discord.Member, required=False)
async def profile(ctx: discord.ApplicationContext, player: discord.Member = None):
    if not db: return await ctx.interaction.response.send_message("Database not connected.", ephemeral=True)
    
    target_user = player or ctx.author
    await ctx.defer()
    
    player_doc = db.collection('players').document(str(target_user.id)).get()
    if not player_doc.exists:
        return await ctx.followup.send(f"{'They are' if player else 'You are'} not registered.", ephemeral=True)

    player_data = player_doc.to_dict()
    username, elo_overall = player_data.get('roblox_username', 'N/A'), get_overall_elo(player_data)
    wins, losses, total = player_data.get('wins', 0), player_data.get('losses', 0), player_data.get('matches_played', 0)
    win_rate = f"{(wins / total * 100):.2f}%" if total > 0 else "N/A"

    embed = discord.Embed(title=f"📊 ELO Profile for {username}", color=target_user.color)
    embed.set_thumbnail(url=target_user.display_avatar.url)
    embed.add_field(name="Overall Stats", value=f"**W/L:** `{wins}`/`{losses}`\n**Win Rate:** `{win_rate}`", inline=False)
    embed.add_field(name="Regional ELO", value=f"**Overall:** `{elo_overall}` (Tier: {get_player_tier(elo_overall)})\n"
              f"**NA:** `{player_data.get('elo_na', STARTING_ELO)}` | **EU:** `{player_data.get('elo_eu', STARTING_ELO)}` | **AS:** `{player_data.get('elo_as', STARTING_ELO)}`", inline=False)
    await ctx.followup.send(embed=embed)

# -------------------------------------
# --- Tournament Commands ---
# -------------------------------------

@tournament.command(name="view", description="View details about the current tournament.")
async def view_tournament(ctx: discord.ApplicationContext):
    if not db: return await ctx.interaction.response.send_message("Database not connected.", ephemeral=True)
    await ctx.defer()

    # Get tournament info from a specific document in Firestore
    tourney_ref = db.collection('config').document('tournament_info')
    tourney_doc = tourney_ref.get()

    if not tourney_doc.exists:
        return await ctx.followup.send("No tournament is currently configured. Ask an admin to create one.", ephemeral=True)

    tourney_data = tourney_doc.to_dict()
    name = tourney_data.get('name', 'N/A')
    description = tourney_data.get('description', 'No description provided.')
    url = tourney_data.get('url', None)

    embed = discord.Embed(title=f"🏆 {name}", description=description, color=discord.Color.purple())
    
    view = discord.ui.View()
    if url and url.startswith("http"):
        # Add a button that links to the Challonge bracket
        view.add_item(discord.ui.Button(label="View Bracket", url=url, style=discord.ButtonStyle.link))
        await ctx.followup.send(embed=embed, view=view)
    else:
        await ctx.followup.send(embed=embed)


# -------------------------------------
# --- Admin Commands ---
# -------------------------------------
admin = elo.create_subgroup("admin", "Admin-only commands for managing the ELO system.")
tournament_admin = tournament.create_subgroup("admin", "Admin commands for tournaments.")

@admin.command(name="set_elo", description="[Admin] Manually set a player's ELO.")
@commands.has_role(ADMIN_ROLE_NAME)
# Options...
async def set_elo(ctx: discord.ApplicationContext, player: discord.Member, region: str, elo_value: int):
    # This command remains the same
    pass # Placeholder for brevity

@admin.command(name="deregister_member", description="[Admin] Deregister a member from the ELO system.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("member", description="The member to deregister.", type=discord.Member, required=True)
async def deregister_member(ctx: discord.ApplicationContext, member: discord.Member):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(member.id))
    if not player_ref.get().exists:
        return await ctx.followup.send(f"**{member.display_name}** is not registered.", ephemeral=True)
    
    player_ref.delete()
    await ctx.followup.send(f"🗑️ Successfully deregistered **{member.display_name}**.", ephemeral=True)

@tournament_admin.command(name="create", description="[Admin] Set or update the current tournament details.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("name", description="The name of the tournament.", required=True)
@discord.option("description", description="A short description of the tournament.", required=True)
@discord.option("challonge_url", description="The full URL to the Challonge bracket.", required=False)
async def create_tournament(ctx: discord.ApplicationContext, name: str, description: str, challonge_url: str = None):
    await ctx.defer(ephemeral=True)

    tourney_data = {
        'name': name,
        'description': description,
        'url': challonge_url,
        'updated_by': ctx.author.name,
        'last_updated': firestore.SERVER_TIMESTAMP
    }
    
    # We store the tournament info in a single, known document for easy retrieval
    db.collection('config').document('tournament_info').set(tourney_data)
    
    await ctx.followup.send("✅ Tournament information has been updated.", ephemeral=True)


@set_elo.error
@deregister_member.error
@create_tournament.error
async def admin_command_error(ctx, error):
    if isinstance(error, commands.MissingRole):
        await ctx.interaction.response.send_message(f"You need the `{ADMIN_ROLE_NAME}` role for this command.", ephemeral=True)
    else:
        print(f"An admin command error occurred: {error}")
        await ctx.interaction.response.send_message("An unexpected error occurred.", ephemeral=True)

# -------------------------------------
# --- Register Commands & Run Bot ---
# -------------------------------------
bot.add_application_command(elo)
bot.add_application_command(tournament)

if __name__ == "__main__":
    if BOT_TOKEN and db:
        bot.run(BOT_TOKEN)
    elif not BOT_TOKEN:
        print("🔴 Bot cannot start: DISCORD_TOKEN environment variable is missing.")
    elif not db:
        print("🔴 Bot cannot start: Database connection failed.")
