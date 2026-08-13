import discord
import os
import asyncio
import aiohttp
from discord.ext import commands
from datetime import datetime
import logging

# ==================== LOGGING ====================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('discord')
logger.setLevel(logging.INFO)

# ==================== CONFIGURATION ====================
TOKEN = os.environ.get('DISCORD_BOT_TOKEN')
GUILD_ID = int(os.environ.get('GUILD_ID', '1271223880975126689'))
API_ENDPOINT = os.environ.get('API_ENDPOINT', 'https://bsyw-profile.vercel.app/api/presence')
API_SECRET = os.environ.get('API_SECRET', 'Bisaya-Presence-2024-SecretKey!')
AFK_CHANNEL_ID = int(os.environ.get('AFK_CHANNEL_ID', '1537088478687531168'))
AFK_TIMEOUT_MINUTES = int(os.environ.get('AFK_TIMEOUT_MINUTES', '5'))

LAVALINK_HOST = os.environ.get('LAVALINK_HOST', os.environ.get('LAVALINK_HOSTNAME', 'localhost'))
LAVALINK_PORT = int(os.environ.get('LAVALINK_PORT', '8080'))
LAVALINK_PASSWORD = os.environ.get('LAVALINK_PASSWORD', 'youshallnotpass')

# ==================== BOT SETUP ====================
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ==================== DATA STORAGE ====================
voice_activity = {}
afk_tasks = {}
music_queues = {}
music_loop = {}

class MusicQueue:
    def __init__(self):
        self.queue = []
        self.current = None
        self.is_playing = False
        self.loop = False
    
    def add(self, track):
        self.queue.append(track)
    
    def next(self):
        if self.queue:
            self.current = self.queue.pop(0)
            return self.current
        self.current = None
        self.is_playing = False
        return None
    
    def clear(self):
        self.queue.clear()
        self.current = None
        self.is_playing = False
    
    def size(self):
        return len(self.queue)
    
    def is_empty(self):
        return len(self.queue) == 0

# ==================== LAVALINK INTEGRATION ====================

class LavalinkClient:
    def __init__(self):
        self.ws = None
        self.session_id = None
        self.player = None
        self.voice_ws = None
        self.connected = False
        self.node_url = f"http://{LAVALINK_HOST}:{LAVALINK_PORT}"
        self.password = LAVALINK_PASSWORD
    
    async def connect(self):
        """Connect to Lavalink"""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                # Try to connect to Lavalink
                async with session.get(f"{self.node_url}/version") as resp:
                    if resp.status == 200:
                        version = await resp.text()
                        logger.info(f"✅ Connected to Lavalink v{version}")
                        self.connected = True
                        return True
            return False
        except Exception as e:
            logger.error(f"❌ Failed to connect to Lavalink: {e}")
            return False
    
    async def load_tracks(self, query):
        """Load tracks from Lavalink"""
        if not self.connected:
            return []
        
        try:
            import aiohttp
            headers = {"Authorization": self.password}
            params = {"identifier": query}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.node_url}/loadtracks",
                    headers=headers,
                    params=params
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and 'tracks' in data:
                            return data['tracks']
            return []
        except Exception as e:
            logger.error(f"Error loading tracks: {e}")
            return []

# Initialize Lavalink client
lavalink = LavalinkClient()

# ==================== MUSIC FUNCTIONS ====================

async def connect_voice(ctx):
    """Connect to voice channel"""
    if not ctx.author.voice:
        return None, "❌ You need to be in a voice channel!"
    
    voice_channel = ctx.author.voice.channel
    
    if ctx.voice_client:
        if ctx.voice_client.channel == voice_channel:
            return ctx.voice_client, None
        await ctx.voice_client.disconnect()
        await asyncio.sleep(1)
    
    try:
        voice_client = await voice_channel.connect(timeout=20.0, reconnect=True)
        await asyncio.sleep(1.5)
        if voice_client and voice_client.is_connected():
            logger.info(f"✅ Connected to {voice_channel.name}")
            return voice_client, None
    except Exception as e:
        return None, f"❌ Failed to connect: {str(e)}"
    
    return None, "❌ Could not connect to voice channel"

async def search_youtube(query):
    """Search YouTube using yt-dlp"""
    import yt_dlp
    import re
    
    YDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'restrictfilenames': True,
        'noplaylist': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
        'extract_flat': False
    }
    
    try:
        youtube_regex = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/'
        
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            if re.match(youtube_regex, query):
                info = ydl.extract_info(query, download=False)
            else:
                search_query = f"ytsearch5:{query}"
                info = ydl.extract_info(search_query, download=False)
            
            if not info:
                return []
            
            tracks = []
            if 'entries' in info:
                for entry in info['entries']:
                    if entry:
                        tracks.append({
                            'title': entry.get('title', 'Unknown'),
                            'url': entry.get('webpage_url', entry.get('url')),
                            'duration': entry.get('duration', 0),
                            'thumbnail': entry.get('thumbnail', ''),
                            'uploader': entry.get('uploader', 'Unknown')
                        })
            else:
                tracks.append({
                    'title': info.get('title', 'Unknown'),
                    'url': info.get('webpage_url', info.get('url')),
                    'duration': info.get('duration', 0),
                    'thumbnail': info.get('thumbnail', ''),
                    'uploader': info.get('uploader', 'Unknown')
                })
            
            return tracks
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        return []

async def get_audio_url(url):
    """Get direct audio URL from YouTube"""
    import yt_dlp
    
    YDL_OPTIONS = {
        'format': 'bestaudio/best',
        'extractaudio': True,
        'audioformat': 'mp3',
        'restrictfilenames': True,
        'noplaylist': False,
        'nocheckcertificate': True,
        'ignoreerrors': False,
        'logtostderr': False,
        'quiet': True,
        'no_warnings': True,
        'default_search': 'auto',
        'source_address': '0.0.0.0',
        'extract_flat': False
    }
    
    try:
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and 'url' in info:
                return info['url']
            return None
    except Exception as e:
        logger.error(f"Error getting audio: {e}")
        return None

async def play_next(ctx, guild_id):
    """Play the next song in queue"""
    if guild_id not in music_queues:
        return
    
    queue = music_queues[guild_id]
    voice_client = ctx.guild.voice_client
    
    if not voice_client or not voice_client.is_connected():
        queue.is_playing = False
        return
    
    if queue.loop and queue.current:
        next_track = queue.current
    else:
        next_track = queue.next()
    
    if not next_track:
        queue.is_playing = False
        await ctx.send("📭 Queue is empty!")
        return
    
    queue.is_playing = True
    
    try:
        FFMPEG_OPTIONS = {
            'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -loglevel quiet',
            'options': '-vn -loglevel quiet'
        }
        
        audio_url = await get_audio_url(next_track['url'])
        
        if not audio_url:
            await ctx.send(f"❌ Could not play: {next_track['title']}")
            queue.is_playing = False
            await play_next(ctx, guild_id)
            return
        
        audio_source = discord.FFmpegPCMAudio(audio_url, **FFMPEG_OPTIONS)
        
        def after_play(error):
            if error:
                logger.error(f"Playback error: {error}")
            asyncio.run_coroutine_threadsafe(
                play_next(ctx, guild_id),
                bot.loop
            )
        
        voice_client.play(audio_source, after=after_play)
        
        embed = discord.Embed(
            title="🎵 Now Playing",
            description=f"**{next_track['title']}**",
            color=discord.Color.blue()
        )
        if next_track.get('thumbnail'):
            embed.set_thumbnail(url=next_track['thumbnail'])
        if next_track.get('duration'):
            minutes = next_track['duration'] // 60
            seconds = next_track['duration'] % 60
            embed.add_field(name="Duration", value=f"{minutes}:{seconds:02d}", inline=True)
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Play error: {e}")
        queue.is_playing = False
        await play_next(ctx, guild_id)

# ==================== MUSIC COMMANDS ====================

@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query):
    """Play a song from YouTube"""
    voice_client, error = await connect_voice(ctx)
    if error:
        await ctx.send(error)
        return
    
    guild_id = ctx.guild.id
    
    if guild_id not in music_queues:
        music_queues[guild_id] = MusicQueue()
        music_loop[guild_id] = False
    
    queue = music_queues[guild_id]
    
    await ctx.send(f"🔍 Searching for: {query}...")
    
    tracks = await search_youtube(query)
    
    if not tracks:
        await ctx.send("❌ No results found!")
        return
    
    for track in tracks:
        track['channel_id'] = ctx.channel.id
        queue.add(track)
    
    if len(tracks) == 1:
        await ctx.send(f"✅ Added to queue: **{tracks[0]['title']}**")
    else:
        await ctx.send(f"✅ Added {len(tracks)} tracks to queue")
    
    if not queue.is_playing:
        await play_next(ctx, guild_id)

@bot.command(name="skip")
async def skip(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing is playing!")
        return
    queue = music_queues[guild_id]
    if not queue.is_playing or not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("❌ Nothing is playing!")
        return
    ctx.voice_client.stop()
    await ctx.send("⏭️ Skipped the current song!")

@bot.command(name="stop")
async def stop(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        queue = music_queues[guild_id]
        queue.clear()
        queue.is_playing = False
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
    await ctx.send("⏹️ Stopped playback and cleared queue!")

@bot.command(name="pause")
async def pause(ctx):
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused the current song!")
    else:
        await ctx.send("❌ Nothing is playing!")

@bot.command(name="resume")
async def resume(ctx):
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed playback!")
    else:
        await ctx.send("❌ Nothing is paused!")

@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("📭 Queue is empty!")
        return
    queue = music_queues[guild_id]
    if queue.is_empty() and not queue.current:
        await ctx.send("📭 Queue is empty!")
        return
    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blue())
    if queue.current and queue.is_playing:
        embed.add_field(name="🎶 Currently Playing", value=f"**{queue.current['title']}**", inline=False)
    if not queue.is_empty():
        queue_text = ""
        for i, track in enumerate(queue.queue[:10], 1):
            queue_text += f"`{i}.` {track['title']}\n"
        if queue_text:
            embed.add_field(name=f"⏭️ Up Next ({len(queue.queue)} tracks)", value=queue_text[:1024], inline=False)
    embed.set_footer(text=f"Queue size: {len(queue.queue)}")
    await ctx.send(embed=embed)

@bot.command(name="loop")
async def loop(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing is playing!")
        return
    queue = music_queues[guild_id]
    queue.loop = not queue.loop
    await ctx.send("🔁 Loop enabled!" if queue.loop else "🔁 Loop disabled!")

@bot.command(name="nowplaying", aliases=["np"])
async def now_playing(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing is playing!")
        return
    queue = music_queues[guild_id]
    if not queue.current or not queue.is_playing:
        await ctx.send("❌ Nothing is playing!")
        return
    track = queue.current
    embed = discord.Embed(title="🎵 Now Playing", description=f"**{track['title']}**", color=discord.Color.blue())
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    if track.get('duration'):
        minutes = track['duration'] // 60
        seconds = track['duration'] % 60
        embed.add_field(name="⏱️ Duration", value=f"{minutes}:{seconds:02d}", inline=True)
    if queue.loop:
        embed.add_field(name="🔁 Loop", value="Enabled", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="clearqueue", aliases=["cq"])
async def clear_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
        await ctx.send("🗑️ Queue cleared!")
    else:
        await ctx.send("📭 Queue is already empty!")

@bot.command(name="leave")
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
        music_queues[guild_id].is_playing = False
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
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
    
    # Connect to Lavalink
    if await lavalink.connect():
        logger.info("✅ Lavalink connection successful!")
    else:
        logger.warning("⚠️ Could not connect to Lavalink, using direct YouTube playback")
    
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
    await ctx.send(embed=embed)

@bot.command(name="help")
async def help_command(ctx):
    embed = discord.Embed(title="🎵 Music Bot Commands", color=discord.Color.blue())
    commands_list = {
        "!play / !p": "Play a song from YouTube",
        "!skip": "Skip the current song",
        "!stop": "Stop playback and clear queue",
        "!pause": "Pause the current song",
        "!resume": "Resume the paused song",
        "!queue / !q": "Show the music queue",
        "!loop": "Toggle loop for current song",
        "!nowplaying / !np": "Show currently playing song",
        "!clearqueue / !cq": "Clear the music queue",
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
    
    print("🚀 Starting bot...")
    bot.run(TOKEN, reconnect=True)
