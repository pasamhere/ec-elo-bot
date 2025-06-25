# main.py
import discord
from discord.ext import commands, tasks
from discord.commands import SlashCommandGroup
import os
import datetime
import pytz # Added for robust timezone handling
import random
import firebase_admin
from firebase_admin import credentials, firestore
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
ADMIN_ROLE_NAME = "Tournament Organizer"
TIER_THRESHOLDS = {
    "S-Tier": 1800,
    "A-Tier": 1600,
    "B-Tier": 1400,
    "C-Tier": 0
}
COUNTRY_FLAGS = {
    'australia': '🇦🇺', 'au': '🇦🇺',
    'united states': '🇺🇸', 'us': '🇺🇸', 'usa': '🇺🇸',
    'united kingdom': '🇬🇧', 'uk': '🇬🇧', 'gb': '🇬🇧',
    'canada': '🇨🇦', 'ca': '🇨🇦',
    'germany': '🇩🇪', 'de': '🇩🇪',
    'france': '🇫🇷', 'fr': '🇫🇷',
    'japan': '🇯🇵', 'jp': '🇯🇵',
    'brazil': '🇧🇷', 'br': '🇧🇷',
    'philippines': '🇵🇭', 'ph': '🇵🇭',
}

bot = commands.Bot(intents=discord.Intents.default())

# -------------------------------------
# --- Views (for buttons) ---
# -------------------------------------
class TournamentSignupView(discord.ui.View):
    def __init__(self, tournament_id: str, role_id: int = None):
        super().__init__(timeout=None) # Persistent view
        self.tournament_id = tournament_id
        self.role_id = role_id

    @discord.ui.button(label="Sign Up", style=discord.ButtonStyle.success, custom_id="tourney_signup_button")
    async def signup_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        player_ref = db.collection('players').document(str(interaction.user.id))
        if not player_ref.get().exists:
            return await interaction.followup.send("You must be registered with `/profile register` first.", ephemeral=True)

        tourney_ref = db.collection('tournaments').document(self.tournament_id)
        
        # Atomically add the user's ID to the participants array
        tourney_ref.update({'participants': firestore.ArrayUnion([str(interaction.user.id)])})
        
        # Assign the participant role if one was set
        if self.role_id:
            try:
                role = interaction.guild.get_role(self.role_id)
                if role and role not in interaction.user.roles:
                    await interaction.user.add_roles(role, reason=f"Signed up for tournament {self.tournament_id}")
            except discord.Forbidden:
                await interaction.followup.send("You have been signed up, but I couldn't assign the participant role. Please check my permissions and role hierarchy!", ephemeral=True)
                return
            except Exception as e:
                print(f"Error assigning role: {e}")

        await interaction.followup.send("You have successfully signed up for the tournament!", ephemeral=True)

# -------------------------------------
# --- Helper Functions & Tasks ---
# -------------------------------------
def get_player_tier(elo):
    for tier, threshold in TIER_THRESHOLDS.items():
        if elo >= threshold: return tier
    return "Unranked"

def calculate_elo_change(winner_data, loser_data):
    winner_elo, loser_elo = get_overall_elo(winner_data), get_overall_elo(loser_data)
    k_factor = K_FACTOR_PROVISIONAL if winner_data.get('matches_played', 0) < PROVISIONAL_MATCHES or loser_data.get('matches_played', 0) < PROVISIONAL_MATCHES else K_FACTOR
    expected_win = 1 / (1 + 10 ** ((loser_elo - winner_elo) / 400))
    return round(k_factor * (1 - expected_win))

def get_overall_elo(player_data):
    return round(sum([player_data.get(r, STARTING_ELO) for r in ['elo_na', 'elo_eu', 'elo_as']]) / 3)

async def update_tier_role(member: discord.Member, new_elo: int):
    if not member: return
    new_tier_name = get_player_tier(new_elo)
    tier_roles = {role.name: role for role in member.guild.roles if role.name in TIER_THRESHOLDS}
    if not tier_roles: return
    new_role = tier_roles.get(new_tier_name)
    if not new_role: return
    roles_to_remove = [role for role in member.roles if role.name in TIER_THRESHOLDS and role.id != new_role.id]
    if roles_to_remove:
        await member.remove_roles(*roles_to_remove, reason="ELO tier update")
    if new_role not in member.roles:
        await member.add_roles(new_role, reason="ELO tier update")

async def process_match_elo(guild, winner_id, loser_id, region, tourney_id=None):
    winner_ref = db.collection('players').document(str(winner_id))
    loser_ref = db.collection('players').document(str(loser_id))
    winner_doc, loser_doc = winner_ref.get(), loser_ref.get()

    if not all([winner_doc.exists, loser_doc.exists]):
        return None, "Winner or loser not found in database."

    winner_data, loser_data = winner_doc.to_dict(), loser_doc.to_dict()
    elo_field = f'elo_{region.lower()}'
    elo_change = calculate_elo_change(winner_data, loser_data)
    
    w_streak = winner_data.get('current_win_streak', 0) + 1
    w_best_streak = max(w_streak, winner_data.get('best_win_streak', 0))
    
    batch = db.batch()
    update_winner = { elo_field: firestore.Increment(elo_change), 'wins': firestore.Increment(1), f'wins_{region}': firestore.Increment(1), 'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP, 'current_win_streak': w_streak, 'best_win_streak': w_best_streak, 'current_loss_streak': 0 }
    update_loser = { elo_field: firestore.Increment(-elo_change), 'losses': firestore.Increment(1), f'losses_{region}': firestore.Increment(1), 'matches_played': firestore.Increment(1), 'last_played_date': firestore.SERVER_TIMESTAMP, 'current_loss_streak': firestore.Increment(1), 'current_win_streak': 0 }
    
    if tourney_id and tourney_id not in winner_data.get('tournaments_participated', []):
        update_winner['tournaments_participated'] = firestore.ArrayUnion([tourney_id])
    if tourney_id and tourney_id not in loser_data.get('tournaments_participated', []):
        update_loser['tournaments_participated'] = firestore.ArrayUnion([tourney_id])
    
    batch.update(winner_ref, update_winner)
    batch.update(loser_ref, update_loser)
    batch.commit()
    
    if guild:
        new_winner_elo = get_overall_elo({**winner_data, elo_field: winner_data.get(elo_field, STARTING_ELO) + elo_change})
        new_loser_elo = get_overall_elo({**loser_data, elo_field: loser_data.get(elo_field, STARTING_ELO) - elo_change})
        await update_tier_role(guild.get_member(int(winner_id)), new_winner_elo)
        await update_tier_role(guild.get_member(int(loser_id)), new_loser_elo)

    match_history_ref = db.collection('match_history').document()
    match_history_ref.set({'winner_id': str(winner_id), 'loser_id': str(loser_id), 'elo_change': elo_change, 'region': region, 'timestamp': firestore.SERVER_TIMESTAMP, 'tournament_id': tourney_id})
    return match_history_ref.id, None

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
        tournaments = db.collection('tournaments').where('status', '==', 'signups_open').stream()
        for tourney in tournaments:
            tourney_data = tourney.to_dict()
            role_id = tourney_data.get('participant_role_id')
            bot.add_view(TournamentSignupView(tourney.id, role_id))
            print(f"Added persistent view for tournament: {tourney.id}")
    else:
        print("🔴 WARNING: Bot is running WITHOUT a database connection.")

# -------------------------------------
# --- Command Groups ---
# -------------------------------------
elo = SlashCommandGroup("elo", "ELO system commands")
stats = SlashCommandGroup("stats", "View detailed player and match statistics")
profile_group = SlashCommandGroup("profile", "Manage and view player profiles")
tournament_group = SlashCommandGroup("tournament", "Manage tournaments")

# -------------------------------------
# --- User Commands ---
# -------------------------------------
@profile_group.command(name="register", description="Register for the ELO system.")
@discord.option("roblox_username", description="Your exact Roblox username.", required=True)
@discord.option("clan", description="Your clan name.", required=True)
@discord.option("country", description="The country you represent (full name or 2-letter code).", required=True)
async def register(ctx: discord.ApplicationContext, roblox_username: str, clan: str, country: str):
    await ctx.defer(ephemeral=True)
    if db.collection('players').document(str(ctx.author.id)).get().exists:
        return await ctx.followup.send("You are already registered!", ephemeral=True)
    new_player_data = {
        'discord_id': str(ctx.author.id), 'discord_name': ctx.author.name, 'roblox_username': roblox_username, 
        'clan': clan, 'country': country, 'elo_na': STARTING_ELO, 'elo_eu': STARTING_ELO, 'elo_as': STARTING_ELO,
        'wins': 0, 'wins_na': 0, 'wins_eu': 0, 'wins_as': 0,
        'losses': 0, 'losses_na': 0, 'losses_eu': 0, 'losses_as': 0,
        'matches_played': 0, 'tournaments_participated': [],
        'last_played_date': firestore.SERVER_TIMESTAMP, 'current_win_streak': 0, 'current_loss_streak': 0, 'best_win_streak': 0
    }
    db.collection('players').document(str(ctx.author.id)).set(new_player_data)
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
    country_name = player_data.get('country', 'N/A')
    flag = COUNTRY_FLAGS.get(country_name.lower(), '🏳️')
    embed.add_field(name="Identity", value=f"{flag} **Country:** {country_name.title()}\n🛡️ **Clan:** {player_data.get('clan') or 'None'}", inline=True)
    wins, losses, total = player_data.get('wins', 0), player_data.get('losses', 0), player_data.get('matches_played', 0)
    win_rate = f"{(wins / total * 100):.2f}%" if total > 0 else "N/A"
    current_streak = player_data.get('current_win_streak', 0) or -player_data.get('current_loss_streak', 0)
    streak_emoji = "🔥" if current_streak > 0 else "🧊"
    streak_str = f"{streak_emoji} {abs(current_streak)}"
    embed.add_field(name="Career Stats", value=f"**W/L:** {wins}/{losses} ({win_rate})\n**Streak:** {streak_str}", inline=True)
    elo_overall = get_overall_elo(player_data)
    embed.add_field(name="ELO Ratings", value=f"**Overall:** `{elo_overall}` (Tier: {get_player_tier(elo_overall)})\n"
              f"**NA:** `{player_data.get('elo_na', STARTING_ELO)}` | **EU:** `{player_data.get('elo_eu', STARTING_ELO)}` | **AS:** `{player_data.get('elo_as', STARTING_ELO)}`", inline=False)
    regional_str = (f"**NA:** {player_data.get('wins_na',0)}W - {player_data.get('losses_na',0)}L\n"
                    f"**EU:** {player_data.get('wins_eu',0)}W - {player_data.get('losses_eu',0)}L\n"
                    f"**AS:** {player_data.get('wins_as',0)}W - {player_data.get('losses_as',0)}L")
    embed.add_field(name="Regional W/L", value=regional_str, inline=False)
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


@elo.command(name="report_match", description="Manually report the result of a match (e.g., 3rd place playoff).")
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


@stats.command(name="h2h", description="View the head-to-head record between two players.")
@discord.option("player1", description="The first player.", type=discord.Member, required=True)
@discord.option("player2", description="The second player.", type=discord.Member, required=True)
async def h2h(ctx: discord.ApplicationContext, player1: discord.Member, player2: discord.Member):
    await ctx.defer()
    p1_wins = len(list(db.collection('match_history').where('winner_id', '==', str(player1.id)).where('loser_id', '==', str(player2.id)).stream()))
    p2_wins = len(list(db.collection('match_history').where('winner_id', '==', str(player2.id)).where('loser_id', '==', str(player1.id)).stream()))
    embed = discord.Embed(title=f"Head-to-Head: {player1.display_name} vs {player2.display_name}", color=discord.Color.teal())
    embed.add_field(name=player1.display_name, value=f"**{p1_wins}** Wins", inline=True)
    embed.add_field(name=player2.display_name, value=f"**{p2_wins}** Wins", inline=True)
    await ctx.followup.send(embed=embed)


@stats.command(name="elo_graph", description="Generate a graph of a player's ELO over time.")
@discord.option("player", description="The player to generate the graph for.", type=discord.Member, required=True)
async def elo_graph(ctx: discord.ApplicationContext, player: discord.Member):
    await ctx.defer()
    winner_query = db.collection('match_history').where('winner_id', '==', str(player.id)).order_by('timestamp', direction='ASCENDING').stream()
    loser_query = db.collection('match_history').where('loser_id', '==', str(player.id)).order_by('timestamp', direction='ASCENDING').stream()
    matches = sorted(list(winner_query) + list(loser_query), key=lambda x: x.to_dict()['timestamp'])
    if not matches:
        return await ctx.followup.send("No match history found for this player to generate a graph.", ephemeral=True)
    timestamps, elo_points, elo_change = [], [], 0
    first_match_data = matches[0].to_dict()
    initial_elo = first_match_data['winner_elo_before'] if first_match_data['winner_id'] == str(player.id) else first_match_data['loser_elo_before']
    timestamps.append(first_match_data['timestamp'] - datetime.timedelta(minutes=1))
    elo_points.append(initial_elo)
    current_elo = initial_elo
    for match_doc in matches:
        match = match_doc.to_dict()
        elo_change = match['elo_change']
        if match['winner_id'] == str(player.id): current_elo += elo_change
        else: current_elo -= elo_change
        timestamps.append(match['timestamp'])
        elo_points.append(current_elo)
    plt.style.use('dark_background')
    fig, ax = plt.subplots()
    ax.plot(timestamps, elo_points, marker='o', linestyle='-', color='#7289DA')
    ax.set_title(f"ELO History for {player.display_name}", color='white')
    ax.set_xlabel("Date", color='white')
    ax.set_ylabel("Overall ELO", color='white')
    ax.grid(True, linestyle='--', alpha=0.3)
    fig.autofmt_xdate()
    buf = io.BytesIO()
    fig.savefig(buf, format='png', bbox_inches='tight', transparent=True)
    buf.seek(0)
    await ctx.followup.send(file=discord.File(buf, 'elo_graph.png'))
    plt.close(fig)


@stats.command(name="tournament_performance", description="View a player's performance in a specific tournament.")
@discord.option("player", description="The player to check.", type=discord.Member, required=True)
@discord.option("tournament_id", description="The ID of the tournament (use /tournament list).", required=True)
async def tournament_performance(ctx: discord.ApplicationContext, player: discord.Member, tournament_id: str):
    await ctx.defer()
    player_doc = db.collection('players').document(str(player.id)).get()
    if not player_doc.exists or tournament_id not in player_doc.to_dict().get('tournaments_participated', []):
        return await ctx.followup.send(f"{player.display_name} did not participate in that tournament.", ephemeral=True)
    
    tourney_doc = db.collection('tournaments').document(tournament_id).get()
    if not tourney_doc.exists:
        return await ctx.followup.send("Could not find a tournament with that ID.", ephemeral=True)
    tournament_name = tourney_doc.to_dict().get("name")

    wins_q = db.collection('match_history').where('tournament_id', '==', tournament_id).where('winner_id', '==', str(player.id)).stream()
    losses_q = db.collection('match_history').where('tournament_id', '==', tournament_id).where('loser_id', '==', str(player.id)).stream()
    wins, losses = len(list(wins_q)), len(list(losses_q))
    
    embed = discord.Embed(title=f"Performance for {player.display_name} in {tournament_name}", color=player.color)
    embed.add_field(name="Record", value=f"**Wins:** {wins}\n**Losses:** {losses}")
    await ctx.followup.send(embed=embed)


# -------------------------------------
# --- Admin Commands ---
# -------------------------------------
@profile_group.command(name="edit", description="Edit a player's registered information.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("member", description="The player to edit.", type=discord.Member, required=True)
@discord.option("new_roblox_username", description="The player's corrected Roblox username.", required=False)
@discord.option("new_clan", description="The player's new clan.", required=False)
@discord.option("new_country", description="The player's new country.", required=False)
async def edit_profile(ctx: discord.ApplicationContext, member: discord.Member, new_roblox_username: str = None, new_clan: str = None, new_country: str = None):
    await ctx.defer(ephemeral=True)
    player_ref = db.collection('players').document(str(member.id))
    if not player_ref.get().exists: return await ctx.followup.send("Player not found.", ephemeral=True)
    update_data = {}
    if new_roblox_username: update_data['roblox_username'] = new_roblox_username
    if new_clan: update_data['clan'] = new_clan
    if new_country: update_data['country'] = new_country
    if not update_data: return await ctx.followup.send("You must provide at least one field to edit.", ephemeral=True)
    player_ref.update(update_data)
    await ctx.followup.send(f"✅ Successfully updated profile for {member.display_name}.", ephemeral=True)

@elo.command(name="revert_match", description="Reverts a match result using its ID.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("match_id", description="The 6-character ID of the match from a player's profile.", required=True)
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
    batch.delete(match_ref)
    batch.commit()
    await ctx.followup.send(f"✅ Successfully reverted Match ID `{match_id}`.", ephemeral=True)

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

@tournament_group.command(name="create", description="Create a new tournament in the database.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("name", description="The name of the tournament.", required=True)
@discord.option("description", description="A short description of the event.", required=True)
@discord.option("start_time", description="Start time. Format: YYYY-MM-DD HH:MM TZ (e.g., 2025-07-04 18:00 EST)", required=True)
@discord.option("rewards", description="Description of the prizes (e.g., 1st: Nitro, 2nd: 1000 Robux).", required=False)
@discord.option("participant_role", description="The role to give to users who sign up.", type=discord.Role, required=False)
@discord.option("bracket_url", description="A link to the tournament bracket.", required=False)
async def create_tournament(ctx: discord.ApplicationContext, name: str, description: str, start_time: str, rewards: str = None, participant_role: discord.Role = None, bracket_url: str = None):
    await ctx.defer(ephemeral=True)
    try:
        parts, dt_naive = start_time.split(), None
        if len(parts) == 3:
            date_str, time_str, tz_str = parts[0], parts[1], parts[2]
            dt_naive = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
            timezone = pytz.timezone(tz_str)
            dt_aware = timezone.localize(dt_naive)
            unix_timestamp = int(dt_aware.timestamp())
        else: raise ValueError("Invalid format")
    except Exception as e:
        await ctx.followup.send(f"Invalid `start_time` format. Please use `YYYY-MM-DD HH:MM TZ` (e.g., `2025-07-04 18:00 EST`).\nError: {e}", ephemeral=True)
        return
    new_tourney_ref = db.collection('tournaments').document()
    new_tourney_ref.set({
        'name': name, 'description': description, 'rewards': rewards,
        'bracket_url': bracket_url, 'status': 'announced', 'participants': [],
        'start_timestamp': unix_timestamp,
        'participant_role_id': participant_role.id if participant_role else None
    })
    await ctx.followup.send(f"✅ Tournament '{name}' created with ID `{new_tourney_ref.id}`. Use this ID to manage it.", ephemeral=True)

@tournament_group.command(name="open_signups", description="Opens signups for a tournament, posting the announcement embed.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("tournament_id", description="The ID of the tournament to open signups for.", required=True)
@discord.option("channel", description="The channel where the announcement should be posted.", type=discord.TextChannel, required=True)
async def open_signups(ctx: discord.ApplicationContext, tournament_id: str, channel: discord.TextChannel):
    await ctx.defer(ephemeral=True)
    tourney_ref = db.collection('tournaments').document(tournament_id)
    tourney_doc = tourney_ref.get()
    if not tourney_doc.exists:
        return await ctx.followup.send("Invalid tournament ID.", ephemeral=True)
    tourney_data = tourney_doc.to_dict()
    embed = discord.Embed(title=f"🏆 Sign-Ups Open for {tourney_data['name']}!", description=tourney_data['description'], color=discord.Color.green())
    if ts := tourney_data.get('start_timestamp'):
        embed.add_field(name="📅 Start Time", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
    if tourney_data.get('rewards'):
        embed.add_field(name="💰 Rewards", value=tourney_data['rewards'], inline=False)
    role_id = tourney_data.get('participant_role_id')
    view = TournamentSignupView(tournament_id, role_id)
    message = await channel.send(embed=embed, view=view)
    tourney_ref.update({'status': 'signups_open', 'signup_message_id': message.id, 'signup_channel_id': channel.id})
    bot.add_view(view, message_id=message.id)
    await ctx.followup.send(f"✅ Sign-up embed has been posted in {channel.mention}.", ephemeral=True)


@tournament_group.command(name="close_signups", description="Closes signups for a tournament.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("tournament_id", description="The ID of the tournament to close.", required=True)
async def close_signups(ctx: discord.ApplicationContext, tournament_id: str):
    await ctx.defer(ephemeral=True)
    tourney_ref = db.collection('tournaments').document(tournament_id)
    tourney_doc = tourney_ref.get()
    if not tourney_doc.exists:
        return await ctx.followup.send("Invalid tournament ID.", ephemeral=True)
    tourney_data = tourney_doc.to_dict()
    tourney_ref.update({'status': 'signups_closed'})
    try:
        if msg_id := tourney_data.get('signup_message_id'):
            channel = await bot.fetch_channel(tourney_data.get('signup_channel_id'))
            message = await channel.fetch_message(msg_id)
            await message.edit(view=None) # Remove buttons
    except Exception as e:
        print(f"Could not disable signup button: {e}")
    await ctx.followup.send(f"✅ Sign-ups for tournament `{tournament_id}` are now closed.", ephemeral=True)

@tournament_group.command(name="archive", description="Archive a tournament, crown winners, and remove participant roles.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("tournament_id", description="The ID of the tournament to archive.", required=True)
@discord.option("first_place", description="The 1st place winner.", type=discord.Member, required=True)
@discord.option("second_place", description="The 2nd place winner.", type=discord.Member, required=True)
@discord.option("third_place", description="The 3rd place winner.", type=discord.Member, required=True)
async def archive_tournament(ctx: discord.ApplicationContext, tournament_id: str, first_place: discord.Member, second_place: discord.Member, third_place: discord.Member):
    await ctx.defer(ephemeral=True)
    tourney_ref = db.collection('tournaments').document(tournament_id)
    tourney_doc = tourney_ref.get()
    if not tourney_doc.exists:
        return await ctx.followup.send("Invalid tournament ID.", ephemeral=True)
    tourney_data = tourney_doc.to_dict()
    if role_id := tourney_data.get('participant_role_id'):
        if role_to_remove := ctx.guild.get_role(role_id):
            for participant_id in tourney_data.get('participants', []):
                try:
                    member = await ctx.guild.fetch_member(int(participant_id))
                    if member and role_to_remove in member.roles:
                        await member.remove_roles(role_to_remove, reason=f"Tournament '{tourney_data['name']}' finished.")
                except Exception as e: print(f"Could not find/derole member {participant_id}: {e}")
    archive_data = {
        'name': tourney_data.get('name'), 'date': firestore.SERVER_TIMESTAMP,
        'first_place_id': str(first_place.id), 'second_place_id': str(second_place.id), 'third_place_id': str(third_place.id),
    }
    db.collection('hall_of_fame').add(archive_data)
    tourney_ref.update({'status': 'archived'})
    await ctx.followup.send(f"✅ Tournament '{archive_data['name']}' has been archived to the Hall of Fame!", ephemeral=True)


# -------------------------------------
# --- Register Commands & Run Bot ---
# -------------------------------------
bot.add_application_command(elo)
bot.add_application_command(profile_group)
bot.add_application_command(stats)
bot.add_application_command(tournament_group)

if __name__ == "__main__":
    if BOT_TOKEN and db:
        bot.run(BOT_TOKEN)
