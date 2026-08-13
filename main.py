import discord
import os
import asyncio
import aiohttp
import yt_dlp
import re
from discord.ext import commands, tasks
from datetime import datetime, timedelta
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

# Spotify API
SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET')

# ==================== YT-DLP OPTIONS ====================
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -loglevel quiet',
    'options': '-vn -loglevel quiet'
}

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
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

# ==================== BOT SETUP ====================
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ==================== DATA STORAGE ====================
voice_activity = {}
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
    
    def remove(self, index):
        if 0 <= index < len(self.queue):
            return self.queue.pop(index)
        return None
    
    def size(self):
        return len(self.queue)
    
    def is_empty(self):
        return len(self.queue) == 0

# ==================== VOICE CONNECTION ====================

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

# ==================== SEARCH FUNCTIONS ====================

async def search_youtube(query, requester):
    """Search YouTube for tracks"""
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
                            'uploader': entry.get('uploader', 'Unknown'),
                            'requester': requester,
                            'source': 'youtube'
                        })
            else:
                tracks.append({
                    'title': info.get('title', 'Unknown'),
                    'url': info.get('webpage_url', info.get('url')),
                    'duration': info.get('duration', 0),
                    'thumbnail': info.get('thumbnail', ''),
                    'uploader': info.get('uploader', 'Unknown'),
                    'requester': requester,
                    'source': 'youtube'
                })
            
            return tracks
    except Exception as e:
        logger.error(f"YouTube search error: {e}")
        return []

async def search_spotify(query, requester):
    """Search Spotify for tracks"""
    tracks = []
    
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return tracks
    
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        
        sp = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET
        ))
        
        if "open.spotify.com" in query:
            # Playlist
            if "playlist" in query:
                playlist_id = query.split("playlist/")[1].split("?")[0]
                results = sp.playlist_tracks(playlist_id)
                for item in results['items']:
                    track = item['track']
                    if track:
                        search_query = f"{track['name']} {track['artists'][0]['name']}"
                        yt_tracks = await search_youtube(search_query, requester)
                        if yt_tracks:
                            yt_tracks[0]['source'] = 'spotify'
                            yt_tracks[0]['spotify_artist'] = track['artists'][0]['name']
                            tracks.append(yt_tracks[0])
            
            # Single track
            elif "track" in query:
                track_id = query.split("track/")[1].split("?")[0]
                result = sp.track(track_id)
                search_query = f"{result['name']} {result['artists'][0]['name']}"
                yt_tracks = await search_youtube(search_query, requester)
                if yt_tracks:
                    yt_tracks[0]['source'] = 'spotify'
                    yt_tracks[0]['spotify_artist'] = result['artists'][0]['name']
                    tracks.append(yt_tracks[0])
            
            # Album
            elif "album" in query:
                album_id = query.split("album/")[1].split("?")[0]
                results = sp.album_tracks(album_id)
                for item in results['items']:
                    search_query = f"{item['name']} {item['artists'][0]['name']}"
                    yt_tracks = await search_youtube(search_query, requester)
                    if yt_tracks:
                        yt_tracks[0]['source'] = 'spotify'
                        yt_tracks[0]['spotify_artist'] = item['artists'][0]['name']
                        tracks.append(yt_tracks[0])
        else:
            # Search query - search Spotify then find on YouTube
            results = sp.search(q=query, type='track', limit=5)
            for item in results['tracks']['items']:
                search_query = f"{item['name']} {item['artists'][0]['name']}"
                yt_tracks = await search_youtube(search_query, requester)
                if yt_tracks:
                    yt_tracks[0]['source'] = 'spotify'
                    yt_tracks[0]['spotify_artist'] = item['artists'][0]['name']
                    tracks.append(yt_tracks[0])
        
        return tracks
        
    except Exception as e:
        logger.error(f"Spotify search error: {e}")
        return []

async def get_audio_url(url):
    """Get direct audio URL from YouTube"""
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
        
        # Send now playing message
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
        if next_track.get('requester'):
            embed.add_field(name="Requested By", value=next_track['requester'], inline=True)
        if next_track.get('source') == 'spotify' and next_track.get('spotify_artist'):
            embed.add_field(name="🎵 Spotify Artist", value=next_track['spotify_artist'], inline=True)
        await ctx.send(embed=embed)
        
    except Exception as e:
        logger.error(f"Play error: {e}")
        queue.is_playing = False
        await play_next(ctx, guild_id)

# ==================== MUSIC COMMANDS ====================

@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query):
    """Play a song from YouTube or Spotify"""
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
    
    tracks = []
    
    # Check if it's a Spotify URL or search
    if "spotify.com" in query or "open.spotify.com" in query:
        tracks = await search_spotify(query, ctx.author.mention)
        if not tracks:
            await ctx.send("❌ No Spotify results found!")
            return
    else:
        tracks = await search_youtube(query, ctx.author.mention)
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
    """Skip the current song"""
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
    """Stop playback and clear the queue"""
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
    """Pause the current song"""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused the current song!")
    else:
        await ctx.send("❌ Nothing is playing!")

@bot.command(name="resume")
async def resume(ctx):
    """Resume the current song"""
    if ctx.voice_client and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ Resumed playback!")
    else:
        await ctx.send("❌ Nothing is paused!")

@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx):
    """Show the current music queue"""
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
            source = "🎵 Spotify" if track.get('source') == 'spotify' else "▶️ YouTube"
            queue_text += f"`{i}.` {track['title']} ({source})\n"
        if queue_text:
            embed.add_field(name=f"⏭️ Up Next ({len(queue.queue)} tracks)", value=queue_text[:1024], inline=False)
    embed.set_footer(text=f"Queue size: {len(queue.queue)}")
    await ctx.send(embed=embed)

@bot.command(name="loop")
async def loop(ctx):
    """Toggle loop for the current song"""
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing is playing!")
        return
    queue = music_queues[guild_id]
    queue.loop = not queue.loop
    await ctx.send("🔁 Loop enabled!" if queue.loop else "🔁 Loop disabled!")

@bot.command(name="nowplaying", aliases=["np"])
async def now_playing(ctx):
    """Show the currently playing song"""
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
    if track.get('uploader'):
        embed.add_field(name="👤 Uploader", value=track['uploader'], inline=True)
    if track.get('requester'):
        embed.add_field(name="📝 Requested By", value=track['requester'], inline=True)
    if track.get('source') == 'spotify' and track.get('spotify_artist'):
        embed.add_field(name="🎵 Spotify Artist", value=track['spotify_artist'], inline=True)
    if queue.loop:
        embed.add_field(name="🔁 Loop", value="Enabled", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="clearqueue", aliases=["cq"])
async def clear_queue(ctx):
    """Clear the music queue"""
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
        await ctx.send("🗑️ Queue cleared!")
    else:
        await ctx.send("📭 Queue is already empty!")

@bot.command(name="remove")
async def remove_from_queue(ctx, position: int):
    """Remove a song from the queue by position"""
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("📭 Queue is empty!")
        return
    removed = music_queues[guild_id].remove(position - 1)
    if removed:
        await ctx.send(f"✅ Removed: **{removed['title']}**")
    else:
        await ctx.send(f"❌ No track at position {position}")

@bot.command(name="shuffle")
async def shuffle_queue(ctx):
    """Shuffle the music queue"""
    import random
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("📭 Queue is empty!")
        return
    queue = music_queues[guild_id]
    if len(queue.queue) < 2:
        await ctx.send("❌ Need at least 2 songs to shuffle!")
        return
    random.shuffle(queue.queue)
    await ctx.send("🔀 Queue shuffled!")

@bot.command(name="leave")
async def leave(ctx):
    """Make the bot leave the voice channel"""
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
        try:
            await member.send(f"You were moved to {afk_channel.name} due to inactivity.")
        except:
            pass
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

# ==================== PRESENCE FUNCTIONS - CONTINUOUS TRACKING ====================

async def update_member_presence(member):
    """Update member presence to your API"""
    try:
        status_map = {
            discord.Status.online: "online",
            discord.Status.idle: "idle",
            discord.Status.dnd: "dnd",
            discord.Status.offline: "offline"
        }
        status = status_map.get(member.status, "offline")
        
        activities = []
        custom_status = None
        
        for activity in member.activities:
            if activity.type == discord.ActivityType.custom:
                custom_status = {
                    "state": activity.state,
                    "emoji": str(activity.emoji) if activity.emoji else None
                }
            elif activity.type == discord.ActivityType.playing:
                activities.append({
                    "type": "game",
                    "name": activity.name,
                    "details": getattr(activity, "details", None),
                    "state": getattr(activity, "state", None)
                })
            elif activity.type == discord.ActivityType.listening:
                if activity.name == "Spotify":
                    activities.append({
                        "type": "spotify",
                        "song": getattr(activity, "title", "Unknown"),
                        "artist": getattr(activity, "artist", "Unknown"),
                        "album": getattr(activity, "album", "Unknown")
                    })
                else:
                    activities.append({"type": "listening", "name": activity.name})
            elif activity.type == discord.ActivityType.watching:
                activities.append({"type": "watching", "name": activity.name})
            elif activity.type == discord.ActivityType.streaming:
                activities.append({
                    "type": "streaming",
                    "name": activity.name,
                    "url": getattr(activity, "url", None)
                })
        
        if activities or custom_status or status != "offline":
            payload = {
                "discord_id": str(member.id),
                "username": member.name,
                "global_name": member.global_name,
                "avatar": str(member.avatar.url) if member.avatar else None,
                "status": status,
                "custom_status": custom_status,
                "activities": activities,
                "last_updated": datetime.now().isoformat()
            }
            
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": API_SECRET, "Content-Type": "application/json"}
                async with session.post(API_ENDPOINT, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        logger.info(f"✅ Updated {member.name}: {status}")
                    else:
                        logger.warning(f"⚠️ API returned {resp.status} for {member.name}")
    except Exception as e:
        logger.error(f"❌ Error updating {member.name}: {e}")

# ==================== CONTINUOUS MEMBER SYNC TASK ====================

@tasks.loop(minutes=5)  # Sync every 5 minutes
async def sync_members():
    """Continuously sync all members' presence"""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        logger.warning("⚠️ Guild not found for sync")
        return
    
    logger.info(f"🔄 Syncing {len(guild.members)} members...")
    
    for member in guild.members:
        if not member.bot:
            try:
                await update_member_presence(member)
            except Exception as e:
                logger.error(f"❌ Error syncing {member.name}: {e}")
        
        # Add small delay to avoid rate limits
        await asyncio.sleep(0.1)
    
    logger.info("✅ Sync complete!")

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
        
        # Initial sync
        logger.info("🔄 Running initial member sync...")
        for member in guild.members:
            if not member.bot:
                await update_member_presence(member)
                await asyncio.sleep(0.1)  # Rate limit prevention
        
        logger.info("✅ Initial sync complete!")
        
        # Start continuous sync task
        sync_members.start()
        logger.info("✅ Continuous member sync started (every 5 minutes)")
    else:
        logger.error(f"❌ Could not find server with ID {GUILD_ID}")

@bot.event
async def on_presence_update(before, after):
    """Triggered when a member's presence changes - REAL-TIME tracking"""
    if not after.bot:
        logger.info(f"🔄 Real-time presence update for {after.name}")
        await update_member_presence(after)

@bot.event
async def on_member_update(before, after):
    """Triggered when a member's anything updates"""
    if not after.bot:
        # Check if presence changed
        if before.status != after.status or before.activities != after.activities:
            logger.info(f"🔄 Member update for {after.name}")
            await update_member_presence(after)

# ==================== BASIC COMMANDS ====================

@bot.command(name="ping")
async def ping(ctx):
    """Check bot latency"""
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latency: {latency}ms")

@bot.command(name="stats")
async def stats(ctx):
    """Show bot statistics"""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await ctx.send("❌ Not connected to server")
        return
    
    total_members = len([m for m in guild.members if not m.bot])
    online = len([m for m in guild.members if m.status != discord.Status.offline and not m.bot])
    voice_members = len([m for m in guild.members if m.voice and m.voice.channel])
    
    embed = discord.Embed(title="📊 Bot Statistics", color=discord.Color.blue())
    embed.add_field(name="👥 Tracked Members", value=str(total_members), inline=True)
    embed.add_field(name="🟢 Online Now", value=str(online), inline=True)
    embed.add_field(name="🎙️ In Voice", value=str(voice_members), inline=True)
    
    # Music stats
    total_queued = sum([music_queues[g].size() for g in music_queues if music_queues[g]])
    embed.add_field(name="🎵 Total Queued", value=str(total_queued), inline=True)
    embed.add_field(name="🎶 Playing", value=sum([1 for g in music_queues if music_queues[g].is_playing]), inline=True)
    
    # Sync status
    embed.add_field(name="🔄 Auto Sync", value="✅ Every 5 minutes", inline=True)
    embed.add_field(name="📡 Real-time Updates", value="✅ Enabled", inline=True)
    
    embed.set_footer(text="Made with ❤️")
    await ctx.send(embed=embed)

@bot.command(name="syncnow")
@commands.has_permissions(administrator=True)
async def sync_now(ctx):
    """Force a manual member sync (Admin only)"""
    await ctx.send("🔄 Forcing manual sync...")
    await sync_members()
    await ctx.send("✅ Manual sync complete!")

@bot.command(name="help")
async def help_command(ctx):
    """Show all commands"""
    embed = discord.Embed(
        title="🎵 Music Bot Commands",
        description="Here are all available commands:",
        color=discord.Color.blue()
    )
    
    commands_list = {
        "!play / !p": "Play a song from YouTube or Spotify",
        "!skip": "Skip the current song",
        "!stop": "Stop playback and clear queue",
        "!pause": "Pause the current song",
        "!resume": "Resume the paused song",
        "!queue / !q": "Show the music queue",
        "!loop": "Toggle loop for current song",
        "!nowplaying / !np": "Show currently playing song",
        "!clearqueue / !cq": "Clear the music queue",
        "!remove": "Remove a song from queue by position",
        "!shuffle": "Shuffle the music queue",
        "!leave": "Bot leaves the voice channel",
        "!ping": "Check bot latency",
        "!stats": "Show bot statistics",
        "!help": "Show this help message",
        "!syncnow": "Force manual member sync (Admin only)"
    }
    
    text = ""
    for cmd, desc in commands_list.items():
        text += f"**{cmd}** - {desc}\n"
    
    embed.add_field(name="📋 Commands", value=text, inline=False)
    embed.set_footer(text="🎶 Enjoy the music! | Auto-sync every 5 minutes")
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send(f"❌ You don't have permission to use this command!")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument: {error}")
    else:
        logger.error(f"Command error: {error}")
        await ctx.send(f"❌ An error occurred: {str(error)}")

# ==================== RUN THE BOT ====================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN not set!")
        exit(1)
    if GUILD_ID == 0:
        print("⚠️ WARNING: GUILD_ID not set! Some features may not work.")
    
    print("🚀 Starting bot...")
    bot.run(TOKEN, reconnect=True)
