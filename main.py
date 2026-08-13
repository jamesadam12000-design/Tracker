import discord
import os
import asyncio
import aiohttp
import yt_dlp
import re
import json
import urllib.parse
from discord.ext import commands, tasks
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

# Spotify API
SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')

# ==================== LAVALINK CONFIGURATION ====================
LAVALINK_HOST = os.environ.get('LAVALINK_HOST', 'lavalink-production-72bc.up.railway.app')
LAVALINK_PORT = int(os.environ.get('LAVALINK_PORT', '8080'))
LAVALINK_PASSWORD = os.environ.get('LAVALINK_PASSWORD', 'youshallnotpass')
LAVALINK_URL = f"http://{LAVALINK_HOST}:{LAVALINK_PORT}"

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
lavalink_session_id = None
lavalink_connected = False

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

# ==================== LAVALINK HTTP API FUNCTIONS ====================

async def lavalink_request(endpoint, method='GET', data=None):
    """Make a request to Lavalink HTTP API"""
    headers = {
        'Authorization': LAVALINK_PASSWORD,
        'Content-Type': 'application/json',
        'Accept': 'application/json'
    }
    
    url = f"{LAVALINK_URL}/{endpoint}"
    
    async with aiohttp.ClientSession() as session:
        try:
            if method == 'GET':
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    elif resp.status == 401:
                        logger.error(f"❌ Lavalink 401 - Wrong password!")
                        return None
                    else:
                        logger.error(f"❌ Lavalink error: {resp.status}")
                        return None
            elif method == 'POST':
                async with session.post(url, headers=headers, json=data) as resp:
                    return resp.status == 200
            elif method == 'PATCH':
                async with session.patch(url, headers=headers, json=data) as resp:
                    return resp.status == 200
            elif method == 'DELETE':
                async with session.delete(url, headers=headers) as resp:
                    return resp.status == 200
        except Exception as e:
            logger.error(f"❌ Lavalink request error: {e}")
            return None

async def lavalink_create_session():
    """Create a Lavalink session"""
    global lavalink_session_id, lavalink_connected
    
    try:
        session_data = {
            "clientName": "DiscordBot",
            "resumingKey": "discord_bot_resume",
            "resumingTimeout": 60
        }
        result = await lavalink_request("sessions", 'POST', session_data)
        if result and 'sessionId' in result:
            lavalink_session_id = result['sessionId']
            lavalink_connected = True
            logger.info(f"✅ Lavalink session created: {lavalink_session_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"❌ Failed to create Lavalink session: {e}")
        return False

async def lavalink_connect_voice(guild_id, channel_id):
    """Connect to voice channel via Lavalink"""
    if not lavalink_session_id:
        return False
    
    try:
        voice_data = {
            "voice": {
                "sessionId": lavalink_session_id,
                "channelId": str(channel_id),
                "guildId": str(guild_id)
            }
        }
        result = await lavalink_request(
            f"sessions/{lavalink_session_id}/players/{guild_id}/voice",
            'PATCH',
            voice_data
        )
        if result:
            logger.info(f"✅ Connected to voice channel via Lavalink")
        return result
    except Exception as e:
        logger.error(f"❌ Lavalink connect voice error: {e}")
        return False

async def lavalink_play_track(guild_id, track_identifier):
    """Play a track using Lavalink"""
    if not lavalink_session_id:
        return False
    
    try:
        # First, load the track
        load_result = await lavalink_request(f"loadtracks?identifier={track_identifier}")
        if not load_result or 'tracks' not in load_result or not load_result['tracks']:
            return False
        
        track = load_result['tracks'][0]
        track_id = track.get('track')
        
        if not track_id:
            return False
        
        # Play the track
        play_data = {
            "track": track_id,
            "noReplace": False
        }
        result = await lavalink_request(
            f"sessions/{lavalink_session_id}/players/{guild_id}/play",
            'POST',
            play_data
        )
        if result:
            logger.info(f"✅ Playing track via Lavalink")
        return result
    except Exception as e:
        logger.error(f"❌ Lavalink play error: {e}")
        return False

async def lavalink_stop_playback(guild_id):
    """Stop playback in Lavalink"""
    if not lavalink_session_id:
        return False
    
    try:
        result = await lavalink_request(
            f"sessions/{lavalink_session_id}/players/{guild_id}/stop",
            'POST'
        )
        return result
    except Exception as e:
        logger.error(f"❌ Lavalink stop error: {e}")
        return False

async def lavalink_pause_playback(guild_id, pause=True):
    """Pause/resume playback in Lavalink"""
    if not lavalink_session_id:
        return False
    
    try:
        result = await lavalink_request(
            f"sessions/{lavalink_session_id}/players/{guild_id}/pause",
            'PATCH',
            {"paused": pause}
        )
        return result
    except Exception as e:
        logger.error(f"❌ Lavalink pause error: {e}")
        return False

async def lavalink_leave_voice(guild_id):
    """Leave voice channel via Lavalink"""
    if not lavalink_session_id:
        return False
    
    try:
        result = await lavalink_request(
            f"sessions/{lavalink_session_id}/players/{guild_id}",
            'DELETE'
        )
        if result:
            logger.info(f"✅ Left voice channel via Lavalink")
        return result
    except Exception as e:
        logger.error(f"❌ Lavalink leave error: {e}")
        return False

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
            elif "track" in query:
                track_id = query.split("track/")[1].split("?")[0]
                result = sp.track(track_id)
                search_query = f"{result['name']} {result['artists'][0]['name']}"
                yt_tracks = await search_youtube(search_query, requester)
                if yt_tracks:
                    yt_tracks[0]['source'] = 'spotify'
                    yt_tracks[0]['spotify_artist'] = result['artists'][0]['name']
                    tracks.append(yt_tracks[0])
        else:
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

async def play_next(ctx, guild_id):
    """Play the next song in queue"""
    if guild_id not in music_queues:
        return
    
    queue = music_queues[guild_id]
    
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
        # Try Lavalink first
        if lavalink_connected:
            success = await lavalink_play_track(guild_id, next_track['url'])
            if success:
                await send_now_playing(ctx, next_track)
                return
        
        # Fallback: Direct YouTube streaming
        audio_url = await get_audio_url(next_track['url'])
        
        if not audio_url:
            await ctx.send(f"❌ Could not play: {next_track['title']}")
            queue.is_playing = False
            await play_next(ctx, guild_id)
            return
        
        voice_client = ctx.guild.voice_client
        if not voice_client or not voice_client.is_connected():
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
        await send_now_playing(ctx, next_track)
        
    except Exception as e:
        logger.error(f"Play error: {e}")
        queue.is_playing = False
        await play_next(ctx, guild_id)

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

async def send_now_playing(ctx, track):
    """Send now playing embed"""
    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**{track['title']}**",
        color=discord.Color.blue()
    )
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    if track.get('duration'):
        minutes = track['duration'] // 60
        seconds = track['duration'] % 60
        embed.add_field(name="Duration", value=f"{minutes}:{seconds:02d}", inline=True)
    if track.get('requester'):
        embed.add_field(name="Requested By", value=track['requester'], inline=True)
    if track.get('source') == 'spotify' and track.get('spotify_artist'):
        embed.add_field(name="🎵 Spotify Artist", value=track['spotify_artist'], inline=True)
    await ctx.send(embed=embed)

# ==================== VOICE CONNECTION ====================

VOICE_CONNECT_BASE_DELAY = 5      # seconds, delay for attempt 1
VOICE_CONNECT_MAX_DELAY = 300     # seconds, cap on backoff
VOICE_CONNECT_MAX_ATTEMPTS = 6    # give up after this many attempts


async def connect_voice_with_backoff(voice_channel, max_attempts=VOICE_CONNECT_MAX_ATTEMPTS):
    """
    Connect to a Discord voice channel with exponential backoff.

    Handles discord.errors.ConnectionClosed with code 4017 (invalid token) by
    tearing down any stale voice client/session state before retrying, so the
    next handshake negotiates a fresh token instead of reusing a stale one.
    Backoff schedule: 5s, 10s, 20s, 40s, 80s, ... capped at 300s.
    """
    guild = voice_channel.guild
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            # Clean up any stale/half-open voice client before attempting a
            # fresh connection so we don't leak unclosed connections.
            existing_vc = guild.voice_client
            if existing_vc is not None:
                try:
                    await existing_vc.disconnect(force=True)
                except Exception as cleanup_error:
                    logger.warning(f"⚠️ Error cleaning up stale voice client: {cleanup_error}")
                await asyncio.sleep(0.5)

            logger.info(
                f"🔄 Voice connect attempt {attempt}/{max_attempts} to '{voice_channel.name}'"
            )
            voice_client = await voice_channel.connect(timeout=20.0, reconnect=True)
            await asyncio.sleep(1.5)

            if voice_client and voice_client.is_connected():
                logger.info(f"✅ Connected to {voice_channel.name} on attempt {attempt}")
                return voice_client, None

            # Got a voice client object but it never actually connected.
            if voice_client:
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    pass
            last_error = "Voice client failed to reach a connected state"

        except discord.errors.ConnectionClosed as e:
            last_error = e
            code = getattr(e, "code", None)
            if code == 4017:
                logger.error(
                    f"❌ Voice WebSocket closed with 4017 (invalid token) on attempt "
                    f"{attempt}/{max_attempts}. Resetting voice session state before retrying."
                )
                # Force-clear stale voice state so Discord issues a fresh
                # token/session on the next handshake instead of reusing one
                # that is already mismatched/expired.
                stale_vc = guild.voice_client
                if stale_vc is not None:
                    try:
                        await stale_vc.disconnect(force=True)
                    except Exception as cleanup_error:
                        logger.warning(f"⚠️ Error during 4017 cleanup: {cleanup_error}")
                try:
                    await guild.change_voice_state(channel=None)
                    await asyncio.sleep(1)
                except Exception as vs_error:
                    logger.warning(f"⚠️ Could not reset voice state: {vs_error}")
            else:
                logger.error(
                    f"❌ Voice WebSocket closed with code {code} on attempt "
                    f"{attempt}/{max_attempts}: {e}"
                )

        except Exception as e:
            last_error = e
            logger.error(
                f"❌ Unexpected error connecting to voice on attempt {attempt}/{max_attempts}: {e}"
            )

        if attempt < max_attempts:
            delay = min(VOICE_CONNECT_BASE_DELAY * (2 ** (attempt - 1)), VOICE_CONNECT_MAX_DELAY)
            logger.info(
                f"⏳ Backing off for {delay}s before retry {attempt + 1}/{max_attempts}"
            )
            await asyncio.sleep(delay)

    logger.error(
        f"❌ Giving up on voice channel '{voice_channel.name}' after {max_attempts} attempts. "
        f"Last error: {last_error}"
    )
    return None, last_error


async def connect_voice(ctx):
    """Connect to voice channel"""
    if not ctx.author.voice:
        return None, "❌ You need to be in a voice channel!"
    
    voice_channel = ctx.author.voice.channel
    guild_id = ctx.guild.id
    
    # Try Lavalink connection first
    if lavalink_connected:
        await lavalink_connect_voice(guild_id, voice_channel.id)
        return "lavalink", None
    
    # Fallback: Discord voice connection
    if ctx.voice_client:
        if ctx.voice_client.channel == voice_channel:
            return ctx.voice_client, None
        await ctx.voice_client.disconnect()
        await asyncio.sleep(1)
    
    voice_client, error = await connect_voice_with_backoff(voice_channel)
    if voice_client:
        return voice_client, None
    
    return None, f"❌ Failed to connect after {VOICE_CONNECT_MAX_ATTEMPTS} attempts: {error}"

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
    if not queue.is_playing:
        await ctx.send("❌ Nothing is playing!")
        return
    
    if lavalink_connected:
        await lavalink_stop_playback(guild_id)
    elif ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
    
    queue.is_playing = False
    await ctx.send("⏭️ Skipped the current song!")

@bot.command(name="stop")
async def stop(ctx):
    """Stop playback and clear the queue"""
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        queue = music_queues[guild_id]
        queue.clear()
        queue.is_playing = False
    
    if lavalink_connected:
        await lavalink_leave_voice(guild_id)
    elif ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
    
    await ctx.send("⏹️ Stopped playback and cleared queue!")

@bot.command(name="pause")
async def pause(ctx):
    """Pause the current song"""
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing is playing!")
        return
    
    if lavalink_connected:
        success = await lavalink_pause_playback(guild_id, True)
        if success:
            await ctx.send("⏸️ Paused the current song!")
        else:
            await ctx.send("❌ Could not pause playback!")
    elif ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ Paused the current song!")
    else:
        await ctx.send("❌ Nothing is playing!")

@bot.command(name="resume")
async def resume(ctx):
    """Resume the current song"""
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing is playing!")
        return
    
    if lavalink_connected:
        success = await lavalink_pause_playback(guild_id, False)
        if success:
            await ctx.send("▶️ Resumed playback!")
        else:
            await ctx.send("❌ Could not resume playback!")
    elif ctx.voice_client and ctx.voice_client.is_paused():
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
    
    if lavalink_connected:
        await lavalink_leave_voice(guild_id)
    elif ctx.voice_client:
        await ctx.voice_client.disconnect()
    
    await ctx.send("👋 Left the voice channel!")

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

@tasks.loop(minutes=5)
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
        
        await asyncio.sleep(0.1)
    
    logger.info("✅ Sync complete!")

# ==================== BOT EVENTS ====================

@bot.event
async def on_ready():
    global lavalink_connected
    
    logger.info(f"✅ {bot.user} is online!")
    logger.info(f"📊 Bot ID: {bot.user.id}")
    logger.info(f"🎵 Music Bot Ready!")
    
    # Connect to Lavalink
    logger.info("🔄 Connecting to Lavalink...")
    if await lavalink_create_session():
        logger.info("✅ Lavalink connected successfully!")
    else:
        logger.warning("⚠️ Lavalink not available - using direct YouTube playback")
    
    guild = bot.get_guild(GUILD_ID)
    if guild:
        logger.info(f"📋 Connected to server: {guild.name}")
        logger.info(f"👥 Members: {len(guild.members)}")
        
        # Initial sync
        logger.info("🔄 Running initial member sync...")
        for member in guild.members:
            if not member.bot:
                await update_member_presence(member)
                await asyncio.sleep(0.1)
        
        logger.info("✅ Initial sync complete!")
        
        # Start continuous sync task
        sync_members.start()
        logger.info("✅ Continuous member sync started (every 5 minutes)")
    else:
        logger.error(f"❌ Could not find server with ID {GUILD_ID}")

@bot.event
async def on_presence_update(before, after):
    if not after.bot:
        logger.info(f"🔄 Real-time presence update for {after.name}")
        await update_member_presence(after)

@bot.event
async def on_member_update(before, after):
    if not after.bot:
        if before.status != after.status or before.activities != after.activities:
            logger.info(f"🔄 Member update for {after.name}")
            await update_member_presence(after)

# ==================== BASIC COMMANDS ====================

@bot.command(name="ping")
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"🏓 Pong! Latency: {latency}ms")

@bot.command(name="lavalink")
async def check_lavalink(ctx):
    """Check Lavalink connection status"""
    if lavalink_connected:
        embed = discord.Embed(
            title="🎧 Lavalink Status",
            description="✅ Lavalink is connected!",
            color=discord.Color.green()
        )
        embed.add_field(name="URL", value=LAVALINK_URL, inline=False)
        embed.add_field(name="Session ID", value=lavalink_session_id or "None", inline=False)
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="🎧 Lavalink Status",
            description="❌ Lavalink is NOT connected - using direct YouTube playback",
            color=discord.Color.orange()
        )
        embed.add_field(name="URL", value=LAVALINK_URL, inline=False)
        embed.add_field(name="Status", value="Direct YouTube mode active", inline=False)
        await ctx.send(embed=embed)

@bot.command(name="stats")
async def stats(ctx):
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
    
    total_queued = sum([music_queues[g].size() for g in music_queues if music_queues[g]])
    embed.add_field(name="🎵 Total Queued", value=str(total_queued), inline=True)
    embed.add_field(name="🎶 Playing", value=sum([1 for g in music_queues if music_queues[g].is_playing]), inline=True)
    
    lavalink_status = "✅ Connected" if lavalink_connected else "🔄 Direct Mode"
    embed.add_field(name="🎧 Audio Mode", value=lavalink_status, inline=True)
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
        "!lavalink": "Check Lavalink connection status",
        "!ping": "Check bot latency",
        "!stats": "Show bot statistics",
        "!syncnow": "Force manual member sync (Admin only)",
        "!help": "Show this help message"
    }
    
    text = ""
    for cmd, desc in commands_list.items():
        text += f"**{cmd}** - {desc}\n"
    
    embed.add_field(name="📋 Commands", value=text, inline=False)
    embed.add_field(
        name="🎧 Audio Mode",
        value="• Lavalink mode (if connected)\n• Direct YouTube mode (fallback)",
        inline=False
    )
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
        print("⚠️ WARNING: GUILD_ID not set!")
    
    print("=" * 50)
    print("🚀 Starting bot...")
    print(f"🎧 Lavalink: {LAVALINK_URL}")
    print("📡 Member tracking: Enabled (auto-sync every 5 minutes)")
    print(f"🎙️ AFK management: {'Enabled' if AFK_CHANNEL_ID else 'Disabled'}")
    print("=" * 50)
    bot.run(TOKEN, reconnect=True)
