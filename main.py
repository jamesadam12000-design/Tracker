import discord
import os
import asyncio
import aiohttp
from discord.ext import commands
from datetime import datetime
import wavelink
import logging

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('wavelink')
logger.setLevel(logging.INFO)

# ==================== CONFIGURATION ====================
TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
GUILD_ID = int(os.environ.get('GUILD_ID', '1271223880975126689'))
API_ENDPOINT = os.environ.get('API_ENDPOINT', 'https://bsyw-profile.vercel.app/api/presence')
API_SECRET = os.environ.get('API_SECRET', 'Bisaya-Presence-2024-SecretKey!')
AFK_CHANNEL_ID = int(os.environ.get('AFK_CHANNEL_ID', '1537088478687531168'))
AFK_TIMEOUT_MINUTES = int(os.environ.get('AFK_TIMEOUT_MINUTES', '5'))

# Lavalink Configuration - PORT 8080 for Railway!
LAVALINK_HOST = os.environ.get('LAVALINK_HOST', os.environ.get('LAVALINK_HOSTNAME', 'localhost'))
LAVALINK_PORT = int(os.environ.get('LAVALINK_PORT', '8080'))  # <-- Changed to 8080
LAVALINK_PASSWORD = os.environ.get('LAVALINK_PASSWORD', 'youshallnotpass')

# Spotify API (Optional)
SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# ==================== BOT SETUP ====================
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.message_content = True
intents.voice_states = True

class MusicBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents, help_command=None)
    
    async def setup_hook(self):
        # Connect to Lavalink on port 8080
        try:
            nodes = [
                wavelink.Node(
                    uri=f"http://{LAVALINK_HOST}:{LAVALINK_PORT}",
                    password=LAVALINK_PASSWORD
                )
            ]
            await wavelink.Pool.connect(nodes=nodes, client=self)
            logger.info(f"✅ Connected to Lavalink at {LAVALINK_HOST}:{LAVALINK_PORT}")
        except Exception as e:
            logger.error(f"❌ Failed to connect to Lavalink: {e}")

bot = MusicBot()

# ==================== DATA STORAGE ====================
voice_activity = {}
afk_tasks = {}

# ==================== LAVALINK PLAYER ====================

class LavalinkPlayer(wavelink.Player):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = []
        self.loop = False
        self.current_track = None
    
    async def do_next(self, ctx):
        """Play the next track in queue"""
        if self.loop and self.current_track:
            track = self.current_track
        elif self.queue:
            track = self.queue.pop(0)
            self.current_track = track
        else:
            self.current_track = None
            return
        
        await self.play(track)
        
        # Send now playing message
        embed = discord.Embed(
            title="🎵 Now Playing",
            description=f"**{track.title}**",
            color=discord.Color.blue()
        )
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        if track.length:
            minutes = track.length // 60000
            seconds = (track.length // 1000) % 60
            embed.add_field(name="Duration", value=f"{minutes}:{seconds:02d}", inline=True)
        if track.extras and track.extras.get('requester'):
            embed.add_field(name="Requested By", value=track.extras.get('requester'), inline=True)
        await ctx.send(embed=embed)

# ==================== MUSIC COMMANDS ====================

@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query):
    """Play a song using Lavalink"""
    if not ctx.author.voice:
        await ctx.send("❌ You need to be in a voice channel!")
        return
    
    voice_channel = ctx.author.voice.channel
    
    # Get or create player
    player = await bot.lavalink.get_player(ctx.guild.id, cls=LavalinkPlayer)
    
    if not player.is_connected:
        await player.connect(voice_channel)
    
    await ctx.send(f"🔍 Searching for: {query}...")
    
    try:
        # Search for tracks
        tracks = await wavelink.Playable.search(query)
        
        if not tracks:
            await ctx.send("❌ No results found!")
            return
        
        # Add tracks to queue
        for track in tracks:
            track.extras = {'requester': ctx.author.mention}
            player.queue.append(track)
        
        if len(tracks) == 1:
            await ctx.send(f"✅ Added to queue: **{tracks[0].title}**")
        else:
            await ctx.send(f"✅ Added {len(tracks)} tracks to queue")
        
        # Start playing if not already
        if not player.is_playing and player.queue:
            await player.do_next(ctx)
            
    except Exception as e:
        await ctx.send(f"❌ Error: {str(e)}")
        logger.error(f"Play error: {e}")

@bot.command(name="skip")
async def skip(ctx):
    """Skip the current song"""
    player = bot.lavalink.get_player(ctx.guild.id)
    if not player or not player.is_playing:
        await ctx.send("❌ Nothing is playing!")
        return
    
    await player.stop()
    await ctx.send("⏭️ Skipped the current song!")

@bot.command(name="stop")
async def stop(ctx):
    """Stop playback and clear queue"""
    player = bot.lavalink.get_player(ctx.guild.id)
    if player:
        player.queue.clear()
        player.current_track = None
        await player.stop()
        await player.disconnect()
    
    await ctx.send("⏹️ Stopped playback and cleared queue!")

@bot.command(name="pause")
async def pause(ctx):
    """Pause the current song"""
    player = bot.lavalink.get_player(ctx.guild.id)
    if not player or not player.is_playing:
        await ctx.send("❌ Nothing is playing!")
        return
    
    await player.pause(True)
    await ctx.send("⏸️ Paused the current song!")

@bot.command(name="resume")
async def resume(ctx):
    """Resume the current song"""
    player = bot.lavalink.get_player(ctx.guild.id)
    if not player:
        await ctx.send("❌ Nothing is playing!")
        return
    
    await player.pause(False)
    await ctx.send("▶️ Resumed playback!")

@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx):
    """Show the current queue"""
    player = bot.lavalink.get_player(ctx.guild.id)
    if not player:
        await ctx.send("📭 Queue is empty!")
        return
    
    if not player.queue and not player.current_track:
        await ctx.send("📭 Queue is empty!")
        return
    
    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blue())
    
    if player.current_track:
        embed.add_field(name="🎶 Currently Playing", value=f"**{player.current_track.title}**", inline=False)
    
    if player.queue:
        queue_text = ""
        for i, track in enumerate(player.queue[:10], 1):
            queue_text += f"`{i}.` {track.title}\n"
        if queue_text:
            embed.add_field(name=f"⏭️ Up Next ({len(player.queue)} tracks)", value=queue_text[:1024], inline=False)
    
    embed.set_footer(text=f"Queue size: {len(player.queue)}")
    await ctx.send(embed=embed)

@bot.command(name="loop")
async def loop(ctx):
    """Toggle loop for the current song"""
    player = bot.lavalink.get_player(ctx.guild.id)
    if not player:
        await ctx.send("❌ Nothing is playing!")
        return
    
    player.loop = not player.loop
    await ctx.send(f"🔁 Loop {'enabled' if player.loop else 'disabled'}!")

@bot.command(name="nowplaying", aliases=["np"])
async def now_playing(ctx):
    """Show currently playing song"""
    player = bot.lavalink.get_player(ctx.guild.id)
    if not player or not player.current_track:
        await ctx.send("❌ Nothing is playing!")
        return
    
    track = player.current_track
    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**{track.title}**",
        color=discord.Color.blue()
    )
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    if track.length:
        minutes = track.length // 60000
        seconds = (track.length // 1000) % 60
        embed.add_field(name="Duration", value=f"{minutes}:{seconds:02d}", inline=True)
    if track.author:
        embed.add_field(name="👤 Artist", value=track.author, inline=True)
    if player.loop:
        embed.add_field(name="🔁 Loop", value="Enabled", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="volume", aliases=["vol"])
async def volume(ctx, vol: int = None):
    """Set volume (1-1000)"""
    player = bot.lavalink.get_player(ctx.guild.id)
    if not player:
        await ctx.send("❌ Nothing is playing!")
        return
    
    if vol is None:
        await ctx.send(f"🔊 Current volume: {player.volume}%")
        return
    
    if vol < 1 or vol > 1000:
        await ctx.send("❌ Volume must be between 1 and 1000!")
        return
    
    await player.set_volume(vol)
    await ctx.send(f"🔊 Volume set to {vol}%")

@bot.command(name="leave")
async def leave(ctx):
    """Make the bot leave the voice channel"""
    player = bot.lavalink.get_player(ctx.guild.id)
    if player:
        player.queue.clear()
        player.current_track = None
        await player.stop()
        await player.disconnect()
        await ctx.send("👋 Left the voice channel!")
    else:
        await ctx.send("❌ I'm not in a voice channel!")

# ==================== AFK FUNCTIONS ====================

async def move_to_afk(member, afk_channel):
    try:
        await member.move_to(afk_channel)
        await asyncio.sleep(0.5)
        await member.edit(mute=True, deafen=True)
        logger.info(f"Moved {member.name} to AFK")
    except Exception as e:
        logger.error(f"Error moving {member.name}: {e}")

@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot or not AFK_CHANNEL_ID:
        return
    
    if after.channel and not before.channel:
        voice_activity[member.id] = {"channel_id": after.channel.id, "last_active": datetime.now()}
    elif not after.channel and before.channel:
        voice_activity.pop(member.id, None)
    elif after.channel and before.channel and after.channel.id != before.channel.id:
        if after.channel.id == AFK_CHANNEL_ID:
            await move_to_afk(member, after.channel)
            return
        voice_activity[member.id] = {"channel_id": after.channel.id, "last_active": datetime.now()}
    
    if after.channel and after.channel.id != AFK_CHANNEL_ID:
        asyncio.create_task(check_afk(member))

async def check_afk(member):
    if not AFK_CHANNEL_ID:
        return
    await asyncio.sleep(AFK_TIMEOUT_MINUTES * 60)
    if not member.voice or not member.voice.channel or member.voice.channel.id == AFK_CHANNEL_ID:
        return
    if member.id in voice_activity:
        last_active = voice_activity[member.id]["last_active"]
        if (datetime.now() - last_active).total_seconds() >= AFK_TIMEOUT_MINUTES * 60:
            afk_channel = member.guild.get_channel(AFK_CHANNEL_ID)
            if afk_channel:
                await move_to_afk(member, afk_channel)

# ==================== PRESENCE FUNCTIONS ====================

async def update_member_presence(member):
    try:
        status_map = {discord.Status.online: "online", discord.Status.idle: "idle", 
                      discord.Status.dnd: "dnd", discord.Status.offline: "offline"}
        status = status_map.get(member.status, "offline")
        activities = []
        
        for activity in member.activities:
            if activity.type == discord.ActivityType.playing:
                activities.append({"type": "game", "name": activity.name})
            elif activity.type == discord.ActivityType.listening:
                if activity.name == "Spotify":
                    activities.append({
                        "type": "spotify",
                        "song": getattr(activity, "title", "Unknown"),
                        "artist": getattr(activity, "artist", "Unknown")
                    })
        
        if activities:
            payload = {
                "discord_id": str(member.id),
                "username": member.name,
                "status": status,
                "activities": activities,
                "last_updated": datetime.now().isoformat()
            }
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": API_SECRET, "Content-Type": "application/json"}
                async with session.post(API_ENDPOINT, json=payload, headers=headers) as resp:
                    pass
    except Exception as e:
        logger.error(f"Error updating {member.name}: {e}")

# ==================== BOT EVENTS ====================

@bot.event
async def on_ready():
    logger.info(f"✅ {bot.user} is online!")
    logger.info(f"📊 Bot ID: {bot.user.id}")
    logger.info(f"🎵 Music Bot Ready!")
    
    guild = bot.get_guild(GUILD_ID)
    if guild:
        logger.info(f"📋 Connected to server: {guild.name}")
        logger.info(f"👥 Members: {len(guild.members)}")
        
        for member in guild.members:
            if not member.bot:
                await update_member_presence(member)
        
        logger.info("✅ Initial sync complete!")

@bot.event
async def on_presence_update(before, after):
    if not after.bot:
        await update_member_presence(after)

# ==================== BASIC COMMANDS ====================

@bot.command(name="ping")
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latency: {latency}ms")

@bot.command(name="stats")
async def stats(ctx):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await ctx.send("❌ Not connected to server")
        return
    
    total_members = len([m for m in guild.members if not m.bot])
    online = len([m for m in guild.members if m.status != discord.Status.offline and not m.bot])
    
    embed = discord.Embed(title="📊 Bot Statistics", color=discord.Color.blue())
    embed.add_field(name="👥 Tracked Members", value=str(total_members), inline=True)
    embed.add_field(name="🟢 Online Now", value=str(online), inline=True)
    
    player = bot.lavalink.get_player(ctx.guild.id)
    if player and player.current_track:
        embed.add_field(name="🎵 Currently Playing", value=player.current_track.title, inline=False)
    
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(title="🎵 Music Bot Commands", color=discord.Color.blue())
    commands_list = {
        "!play / !p": "Play a song from YouTube or Spotify",
        "!skip": "Skip the current song",
        "!stop": "Stop playback and clear queue",
        "!pause": "Pause the current song",
        "!resume": "Resume the paused song",
        "!queue / !q": "Show the music queue",
        "!loop": "Toggle loop for current song",
        "!nowplaying / !np": "Show currently playing song",
        "!volume / !vol": "Set volume (1-1000)",
        "!leave": "Bot leaves the voice channel",
        "!ping": "Check bot latency",
        "!stats": "Show bot statistics"
    }
    text = ""
    for cmd, desc in commands_list.items():
        text += f"**{cmd}** - {desc}\n"
    embed.add_field(name="📋 Commands", value=text, inline=False)
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error(f"Command error: {error}")
    await ctx.send(f"❌ Error: {str(error)}")

# ==================== RUN ====================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN not set!")
        exit(1)
    if GUILD_ID == 0:
        print("⚠️ WARNING: GUILD_ID not set!")
    
    print("🚀 Starting bot with Lavalink on port 8080...")
    bot.run(TOKEN, reconnect=True)
