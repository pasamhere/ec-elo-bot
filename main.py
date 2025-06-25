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
    "S-Tier": 1800, "A-Tier": 1600, "B-Tier": 1400, "C-Tier": 0
}
COUNTRY_FLAGS = {
    'australia': '🇦🇺', 'au': '🇦🇺', 'united states': '🇺🇸', 'us': '🇺🇸', 'usa': '🇺🇸',
    'united kingdom': '🇬🇧', 'uk': '🇬🇧', 'gb': '🇬🇧', 'canada': '🇨🇦', 'ca': '🇨🇦',
    'germany': '🇩🇪', 'de': '🇩🇪', 'france': '🇫🇷', 'fr': '🇫🇷', 'japan': '🇯🇵', 'jp': '🇯🇵',
    'brazil': '🇧🇷', 'br': '🇧🇷', 'philippines': '🇵🇭', 'ph': '🇵🇭',
}

bot = commands.Bot(intents=discord.Intents.default())

# -------------------------------------
# --- Views (for buttons) ---
# -------------------------------------
class TournamentSignupView(discord.ui.View):
    def __init__(self, tournament_id: str, role_id: int = None):
        super().__init__(timeout=None)
        self.tournament_id = tournament_id
        self.role_id = role_id

    @discord.ui.button(label="Sign Up", style=discord.ButtonStyle.success, custom_id="tourney_signup_button")
    async def signup_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        player_ref = db.collection('players').document(str(interaction.user.id))
        if not player_ref.get().exists:
            return await interaction.followup.send("You must be registered with `/profile register` first.", ephemeral=True)

        tourney_ref = db.collection('tournaments').document(self.tournament_id)
        tourney_ref.update({'participants': firestore.ArrayUnion([str(interaction.user.id)])})
        
        # Assign the participant role if one was set
        if self.role_id:
            try:
                role = interaction.guild.get_role(self.role_id)
                if role:
                    await interaction.user.add_roles(role, reason=f"Signed up for tournament {self.tournament_id}")
            except discord.Forbidden:
                await interaction.followup.send("You have been signed up, but I couldn't assign the participant role. Please check my permissions!", ephemeral=True)
                return
            except Exception as e:
                print(f"Error assigning role: {e}")

        await interaction.followup.send("You have successfully signed up for the tournament!", ephemeral=True)

# -------------------------------------
# --- Helper Functions & Tasks ---
# -------------------------------------
# All previous helper functions are unchanged...
def get_player_tier(elo): pass
def calculate_elo_change(winner_data, loser_data): pass
def get_overall_elo(player_data): pass
async def update_tier_role(member: discord.Member, new_elo: int): pass
async def process_match_elo(winner_id, loser_id, region, tourney_id=None): pass
@tasks.loop(hours=24)
async def daily_elo_decay(): pass

# -------------------------------------
# --- Bot Events ---
# -------------------------------------
@bot.event
async def on_ready():
    print(f'✅ Bot is ready and logged in as {bot.user}')
    if db:
        print("☁️  Connected to Firestore database.")
        daily_elo_decay.start()
        # Add persistent views for any active tournament signups
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
# All user commands like /profile register, /profile view, /stats h2h, etc. are unchanged
# ...

# -------------------------------------
# --- Admin Commands ---
# -------------------------------------
# All admin commands like /profile edit, /elo revert_match, etc. are unchanged
# ...

@tournament_group.command(name="create", description="Create a new tournament in the database.")
@commands.has_role(ADMIN_ROLE_NAME)
@discord.option("name", description="The name of the tournament.", required=True)
@discord.option("description", description="A short description of the event.", required=True)
@discord.option("start_time", description="The start time. Format: YYYY-MM-DD HH:MM TZ (e.g., 2025-07-04 18:00 EST)", required=True)
@discord.option("rewards", description="Description of the prizes (e.g., 1st: Nitro, 2nd: 1000 Robux).", required=False)
@discord.option("participant_role", description="The role to give to users who sign up.", type=discord.Role, required=False)
@discord.option("bracket_url", description="A link to the Challonge/tournament bracket.", required=False)
async def create_tournament(ctx: discord.ApplicationContext, name: str, description: str, start_time: str, rewards: str = None, participant_role: discord.Role = None, bracket_url: str = None):
    await ctx.defer(ephemeral=True)
    
    # Parse the timestamp
    try:
        # A simple parser for "YYYY-MM-DD HH:MM TZ" format
        parts = start_time.split()
        date_str, time_str, tz_str = parts[0], parts[1], parts[2]
        dt_naive = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        
        # Handle timezone
        timezone = pytz.timezone(tz_str)
        dt_aware = timezone.localize(dt_naive)
        unix_timestamp = int(dt_aware.timestamp())
    except Exception as e:
        await ctx.followup.send(f"Invalid `start_time` format. Please use `YYYY-MM-DD HH:MM TZ` (e.g., `2025-07-04 18:00 EST`).\nError: {e}", ephemeral=True)
        return

    new_tourney_ref = db.collection('tournaments').document()
    tourney_data = {
        'name': name, 'description': description, 'rewards': rewards,
        'bracket_url': bracket_url, 'status': 'announced', 'participants': [],
        'start_timestamp': unix_timestamp,
        'participant_role_id': participant_role.id if participant_role else None
    }
    new_tourney_ref.set(tourney_data)
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
    
    # Create the detailed embed
    embed = discord.Embed(title=f"🏆 {tourney_data['name']}", description=tourney_data['description'], color=discord.Color.green())
    
    # Add Hammertime timestamps
    ts = tourney_data.get('start_timestamp')
    if ts:
        embed.add_field(name="📅 Start Time", value=f"<t:{ts}:F> (<t:{ts}:R>)", inline=False)
        
    if tourney_data.get('rewards'):
        embed.add_field(name="💰 Rewards", value=tourney_data['rewards'], inline=False)
    
    role_id = tourney_data.get('participant_role_id')
    view = TournamentSignupView(tournament_id, role_id)
    
    message = await channel.send(embed=embed, view=view)
    
    tourney_ref.update({'status': 'signups_open', 'signup_message_id': message.id})
    bot.add_view(view, message_id=message.id) # Ensure view persists after restart
    await ctx.followup.send(f"✅ Sign-up embed has been posted in {channel.mention}.", ephemeral=True)

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

    # --- NEW: Role Removal Logic ---
    role_id = tourney_data.get('participant_role_id')
    participants = tourney_data.get('participants', [])
    
    if role_id and participants:
        role_to_remove = ctx.guild.get_role(role_id)
        if role_to_remove:
            removed_count = 0
            for participant_id in participants:
                try:
                    member = await ctx.guild.fetch_member(int(participant_id))
                    if member and role_to_remove in member.roles:
                        await member.remove_roles(role_to_remove, reason=f"Tournament '{tourney_data['name']}' finished.")
                        removed_count += 1
                except discord.NotFound:
                    print(f"Could not find member with ID {participant_id} to remove role.")
                except discord.Forbidden:
                    await ctx.followup.send(f"⚠️ I don't have permission to remove the '{role_to_remove.name}' role. Please check my role hierarchy.", ephemeral=True)
                    # We continue even if we fail to remove roles
                    break # Stop trying to remove roles if permissions are wrong
                except Exception as e:
                    print(f"An unexpected error occurred removing role: {e}")
            print(f"Removed participant role from {removed_count} members.")

    # --- Archiving Logic ---
    archive_data = {
        'name': tourney_data.get('name'),
        'date': firestore.SERVER_TIMESTAMP,
        'first_place_id': str(first_place.id),
        'second_place_id': str(second_place.id),
        'third_place_id': str(third_place.id),
    }
    db.collection('hall_of_fame').add(archive_data)
    tourney_ref.update({'status': 'archived'})
    
    await ctx.followup.send(f"✅ Tournament '{archive_data['name']}' has been archived to the Hall of Fame!", ephemeral=True)


# ... (all other commands remain)

# -------------------------------------
# --- Register Commands & Run Bot ---
# -------------------------------------
# ... (all command registrations remain)
