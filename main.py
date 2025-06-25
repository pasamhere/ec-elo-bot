# main.py
import discord
from discord.ext import commands, tasks
from discord.commands import SlashCommandGroup
import os
import datetime
import firebase_admin
from firebase_admin import credentials, firestore
import pychallonge

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
challonge = SlashCommandGroup("challonge", "Challonge integration commands")
tournament = SlashCommandGroup("tournament", "Tournament management commands")

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

@profile_group.command(name="deregister", description="Deregister yourself from the ELO system. This is permanent!")
async def deregister(ctx: discord.ApplicationContext):
    player_ref = db.collection('players').document(str(ctx.author.id))
    if not player_ref.get().exists:
        return await ctx.interaction.response.send_message("You are not registered.", ephemeral=True)
    view = DeregisterView(ctx.author)
    await ctx.interaction.response.send_message("Are you sure you want to deregister?", view=view, ephemeral=True)
    await view.wait()
    if view.confirmed:
        player_ref.delete()
        await ctx.followup.send("You have been deregistered.", ephemeral=True)
    else:
        await ctx.followup.send("Deregistration cancelled.", ephemeral=True)


@profile_group.command(name="view", description="View your or another player's ELO profile.")
@discord.option("player", description="The player whose profile you want to see.", type=discord.Member, required=False)
async def view_profile(ctx: discord.ApplicationContext, player: discord.Member = None):
    target_user = player or ctx.author
    await ctx.defer()
    player_doc = db.collection('players').document(str(target_user.id)).get()
    if not player_doc.exists:
        return await ctx.followup.send("That player is not registered.", ephemeral=True)
    
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
@discord.option("winner", description="The Discord user who won.", type=discord.Member, required=True)
@discord.option("loser", description="The Discord user who lost.", type=discord.Member, required=True)
@discord.option("region", description="The region the match was played in.", choices=["NA", "EU", "AS"], required=True)
async def report_match(ctx: discord.ApplicationContext, winner: discord.Member, loser: discord.Member, region: str):
    await ctx.defer()
    match_id, error = await process_match_elo(winner.id, loser.id, region)
    if error:
        return await ctx.followup.send(f"Error: {error}", ephemeral=True)
    await ctx.followup.send(f"✅ Match manually recorded! Match ID: `{match_id}`")

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
    medals = ["🥇", "🥈", "🥉"]
    lb_string = ""
    for i, player in enumerate(sorted_players[:10]):
        rank_display = medals[i] if i < 3 else f"`#{i+1: <2}`"
        elo_score = get_overall_elo(player) if region == "Overall" else player.get(f'elo_{region.lower()}', STARTING_ELO)
        lb_string += f"{rank_display} **{player.get('roblox_username', 'Unknown')}** - `{elo_score}` ELO (Tier: {get_player_tier(elo_score)})\n"
    embed.add_field(name="Top 10 Rankings", value=lb_string or "No players found.", inline=False)
    await ctx.followup.send(embed=embed)

@challonge.command(name="link_account", description="Set a Challonge name ONLY if it's different from your Roblox name.")
@discord.option("username", description="Your exact Challonge username.", required=True)
async def link_account(ctx: discord.ApplicationContext, username: str):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(ctx.author.id))
    if not player_ref.get().exists:
        return await ctx.followup.send("You must be registered first (`/profile register`).", ephemeral=True)
    player_ref.update({'challonge_username': username})
    await ctx.followup.send(f"✅ Your Challonge username has been set to **{username}** for override purposes.", ephemeral=True)

@tournament.command(name="view", description="View details about the current tournament.")
async def view_tournament(ctx: discord.ApplicationContext):
    await ctx.defer()
    tourney_ref = db.collection('config').document('tournament_info')
    tourney_doc = tourney_ref.get()
    if not tourney_doc.exists:
        return await ctx.followup.send("No tournament is currently configured.", ephemeral=True)
    tourney_data = tourney_doc.to_dict()
    embed = discord.Embed(title=f"🏆 {tourney_data.get('name', 'N/A')}", description=tourney_data.get('description', 'No description.'), color=discord.Color.purple())
    view = discord.ui.View()
    if tourney_data.get('url', '').startswith("http"):
        view.add_item(discord.ui.Button(label="View Bracket", url=tourney_data.get('url'), style=discord.ButtonStyle.link))
    await ctx.followup.send(embed=embed, view=view)

# -------------------------------------
# --- Admin Commands ---
# -------------------------------------
@elo.command(name="revert_match", description="[Admin] Reverts a match result using its ID.")
@commands.has_role(ADMIN_ROLE_NAME)
async def revert_match(ctx, match_id): pass # Placeholder for brevity

@profile_group.command(name="edit", description="[Admin] Edit a player's registered information.")
@commands.has_role(ADMIN_ROLE_NAME)
async def edit_profile(ctx, member, new_roblox_username, new_clan, new_country): pass # Placeholder

@challonge.command(name="set_api_key", description="[Admin] Securely set the Challonge API key for the bot.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("api_key", description="Your Challonge API key.", required=True)
async def set_api_key(ctx: discord.ApplicationContext, api_key: str):
    await ctx.defer(ephemeral=True)
    db.collection('config').document('challonge_api').set({'key': api_key, 'set_by': ctx.author.name})
    await ctx.followup.send("✅ Challonge API Key has been set.", ephemeral=True)

@challonge.command(name="link", description="[Admin] Manually link a member's Challonge username.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("member", type=discord.Member, required=True)
@discord.option("challonge_name", required=True)
async def admin_link_challonge(ctx, member, challonge_name):
    await ctx.defer(ephemeral=True)
    db.collection('players').document(str(member.id)).update({'challonge_username': challonge_name})
    await ctx.followup.send(f"✅ Linked **{member.display_name}** to Challonge name **{challonge_name}**.", ephemeral=True)

@challonge.command(name="import_matches", description="[Admin] Import matches from a Challonge tournament.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("tournament_url", required=True)
@discord.option("region", choices=["NA", "EU", "AS"], required=True)
async def import_matches(ctx, tournament_url, region):
    await ctx.defer(ephemeral=True)
    api_key_doc = db.collection('config').document('challonge_api').get()
    if not api_key_doc.exists:
        return await ctx.followup.send("API key not set. Use `/challonge set_api_key`.", ephemeral=True)
    
    all_players = db.collection('players').stream()
    player_map = {p.to_dict().get(k,'').lower(): p.id for p in all_players for k in ('roblox_username','challonge_username') if p.to_dict().get(k)}

    try:
        challonge.set_credentials("", api_key_doc.to_dict().get('key'))
        tourney_id = tournament_url.split('/')[-1] or tournament_url.split('/')[-2]
        tournament = challonge.tournaments.show(tourney_id)
        matches = challonge.matches.index(tournament['id'], state="complete")
    except Exception as e:
        return await ctx.followup.send(f"Error fetching from Challonge: {e}", ephemeral=True)

    success_count, failed = 0, []
    for match in matches:
        winner = challonge.participants.show(tournament['id'], match['winner_id'])
        loser = challonge.participants.show(tournament['id'], match['loser_id'])
        winner_name = winner.get('username') or winner.get('name')
        loser_name = loser.get('username') or loser.get('name')
        winner_id, loser_id = player_map.get(winner_name.lower()), player_map.get(loser_name.lower())
        
        if not (winner_id and loser_id):
            failed.append(f"'{winner_name}' vs '{loser_name}'")
            continue
        
        _, error = await process_match_elo(winner_id, loser_id, region, tournament['name'])
        if error: failed.append(f"'{winner_name}' vs '{loser_name}' ({error})")
        else: success_count += 1
            
    embed = discord.Embed(title="Challonge Import Summary", color=discord.Color.green() if not failed else discord.Color.orange())
    embed.add_field(name="Imported", value=f"`{success_count}` matches")
    if failed: embed.add_field(name="Failed/Skipped", value="\n".join(failed)[:1024])
    await ctx.followup.send(embed=embed)


# -------------------------------------
# --- Register Commands & Run Bot ---
# -------------------------------------
bot.add_application_command(elo)
bot.add_application_command(stats)
bot.add_application_command(profile_group)
bot.add_application_command(challonge)
bot.add_application_command(tournament)

if __name__ == "__main__":
    if BOT_TOKEN and db:
        bot.run(BOT_TOKEN)
