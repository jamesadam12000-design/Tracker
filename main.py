import discord
import os
import asyncio
import aiohttp
import yt_dlp
import re
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

# ==================== LAVALINK ====================
# Railway networking has two totally different address forms and Lavalink
# needs the RIGHT one:
#
#  A) PRIVATE/INTERNAL (works only if bot + Lavalink are services in the
#     SAME Railway project) - fastest, no public exposure needed:
#       host: <service-name>.railway.internal   (check Lavalink service's
#             Settings -> Networking -> Private Networking)
#       port: whatever Lavalink actually listens on internally (usually 8080)
#       scheme: http  (private network traffic is not TLS)
#
#  B) PUBLIC DOMAIN (needed if they're in different projects, or you
#     haven't enabled private networking):
#       host: lavalink-production-43a7.up.railway.app
#       port: NONE - Railway's edge only terminates on 443/HTTPS
#       scheme: https
#
# This code tries whichever candidates you give it via env vars, in order,
# and uses the first one that actually answers /version. Set LAVALINK_HOST
# to a comma-separated list to have it try more than one automatically.

# Lavalink's Railway service exposes its auth password as
# LAVALINK_SERVER_PASSWORD (matches the application.yml key
# lavalink.server.password), not LAVALINK_PASSWORD - read the right one.
LAVALINK_PASSWORD = os.environ.get('LAVALINK_SERVER_PASSWORD', 'youshallnotpass').strip()

def _build_candidates():
    """
    Build an ordered list of (url) candidates to try.
    Reads LAVALINK_HOST (comma-separated allowed), LAVALINK_PORT,
    LAVALINK_SSL as overrides. If LAVALINK_HOST is unset, falls back to a
    sane default list covering both private and public addressing.
    """
    raw_host = os.environ.get('LAVALINK_HOST', '').strip()
    raw_port = os.environ.get('LAVALINK_PORT', '').strip()
    raw_ssl = os.environ.get('LAVALINK_SSL', '').strip().lower()

    candidates = []

    if raw_host:
        hosts = [h.strip() for h in raw_host.split(',') if h.strip()]
        for h in hosts:
            is_internal = h.endswith('.railway.internal')
            scheme = 'http' if is_internal else 'https'
            if raw_ssl:
                scheme = 'https' if raw_ssl == 'true' else 'http'
            port = raw_port if raw_port else (None if not is_internal else '8080')
            url = f"{scheme}://{h}:{port}" if port else f"{scheme}://{h}"
            candidates.append(url)
    else:
        # No host configured at all - try private networking first (faster,
        # bypasses any public-edge access restrictions), then fall back to
        # the public domain.
        candidates.append("http://lavalink.railway.internal:8080")
        candidates.append("https://lavalink-production-43a7.up.railway.app")

    # de-dupe while preserving order
    seen = set()
    ordered = []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered

LAVALINK_CANDIDATES = _build_candidates()
LAVALINK_URL = LAVALINK_CANDIDATES[0]  # active URL, updated once a candidate succeeds

# no timeout previously set on aiohttp requests, so a bad host/port would
# hang for minutes before failing. Cap it so startup fails fast.
LAVALINK_TIMEOUT = aiohttp.ClientTimeout(total=8)

logger.info("=" * 50)
logger.info(f"🎧 Lavalink candidates to try: {LAVALINK_CANDIDATES}")
logger.info(f"🔑 Password (repr, to catch hidden whitespace/quotes): {LAVALINK_PASSWORD!r}")
logger.info(f"🔑 Password length: {len(LAVALINK_PASSWORD)}")
logger.info("=" * 50)

# ==================== YT-DLP ====================
YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': False,
    'nocheckcertificate': True,
    'ignoreerrors': True,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': False,
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'extractor_args': {
        'youtube': {
            'player_client': ['android'],
            'skip': ['dash', 'hls'],
        }
    }
}

# ==================== BOT ====================
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ==================== DATA ====================
voice_activity = {}
music_queues = {}
lavalink_session_id = None
lavalink_connected = False
lavalink_retry_count = 0

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

# ==================== LAVALINK API ====================

async def lavalink_request(endpoint, method='GET', data=None, base_url=None):
    """Make a request to Lavalink"""
    headers = {
        'Authorization': LAVALINK_PASSWORD,
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        # Railway's edge (and many reverse proxies) block requests whose
        # User-Agent looks like a bare script/library (aiohttp's default
        # is something like "Python/3.x aiohttp/3.x"), returning 403
        # before the request even reaches Lavalink. A normal browser-style
        # UA avoids that filter.
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    url = f"{base_url or LAVALINK_URL}/{endpoint}"

    # timeout added so a bad host/port fails in ~8s instead of hanging
    async with aiohttp.ClientSession(timeout=LAVALINK_TIMEOUT) as session:
        try:
            if method == 'GET':
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    body_text = await resp.text()
                    if resp.status == 403:
                        logger.error(
                            f"❌ Lavalink GET {endpoint} -> HTTP 403. Response body: {body_text[:500]!r}"
                        )
                        return None
                    logger.error(f"❌ Lavalink GET {endpoint} -> HTTP {resp.status}. Body: {body_text[:500]!r}")
                    return None
            elif method == 'POST':
                async with session.post(url, headers=headers, json=data) as resp:
                    if resp.status not in (200, 204):
                        body = await resp.text()
                        logger.error(f"❌ Lavalink POST {endpoint} -> HTTP {resp.status}: {body[:300]}")
                        return False
                    # Some POST endpoints (e.g. session creation) return a JSON body
                    if resp.content_type == 'application/json':
                        try:
                            return await resp.json()
                        except Exception:
                            return True
                    return True
            elif method == 'PATCH':
                async with session.patch(url, headers=headers, json=data) as resp:
                    if resp.status not in (200, 204):
                        body = await resp.text()
                        logger.error(f"❌ Lavalink PATCH {endpoint} -> HTTP {resp.status}: {body[:300]}")
                        return False
                    return True
            elif method == 'DELETE':
                async with session.delete(url, headers=headers) as resp:
                    return resp.status in (200, 204)
        except aiohttp.ClientConnectorError as e:
            logger.error(f"❌ Cannot connect to Lavalink at {LAVALINK_URL}: {e}")
            return None
        except asyncio.TimeoutError:
            logger.error(f"❌ Lavalink request timed out: {url}")
            return None
        except Exception as e:
            logger.error(f"❌ Lavalink error: {e}")
            return None

async def lavalink_init():
    """
    Try every candidate URL in order. The first one that answers /version
    becomes the active LAVALINK_URL for the rest of the bot's lifetime.
    """
    global lavalink_session_id, lavalink_connected, lavalink_retry_count, LAVALINK_URL

    for candidate in LAVALINK_CANDIDATES:
        try:
            logger.info(f"🔍 Trying Lavalink at {candidate} ...")
            version = await lavalink_request("version", base_url=candidate)
            if not version:
                logger.warning(f"⚠️ No response from {candidate}")
                continue

            logger.info(f"✅ Lavalink version at {candidate}: {version}")

            # Create session - try v4 path first, fall back to legacy path
            result = await lavalink_request("v4/sessions", 'POST', {
                "clientName": "DiscordBot",
                "resumingKey": "discord_bot",
                "resumingTimeout": 60
            }, base_url=candidate)

            if not (result and isinstance(result, dict) and 'sessionId' in result):
                result = await lavalink_request("sessions", 'POST', {
                    "clientName": "DiscordBot",
                    "resumingKey": "discord_bot",
                    "resumingTimeout": 60
                }, base_url=candidate)

            if result and isinstance(result, dict) and 'sessionId' in result:
                lavalink_session_id = result['sessionId']
                lavalink_connected = True
                LAVALINK_URL = candidate  # lock in the working address
                logger.info(f"✅ Lavalink connected at {candidate}! Session: {lavalink_session_id}")
                return True
            else:
                logger.error(f"❌ {candidate} answered /version but session creation failed")

        except Exception as e:
            logger.error(f"❌ Lavalink init error on {candidate}: {e}")
            continue

    logger.error("❌ None of the Lavalink candidates worked.")
    return False

async def lavalink_join(guild_id, channel_id):
    if not lavalink_session_id:
        return False
    try:
        # Note: proper Discord voice join requires a voice WS token/endpoint
        # from Discord's own gateway (via on_voice_server_update), which this
        # simplified flow doesn't currently capture. If playback fails after
        # Lavalink connects successfully, this is the next thing to fix.
        result = await lavalink_request(
            f"sessions/{lavalink_session_id}/players/{guild_id}/voice",
            'PATCH',
            {"voice": {"sessionId": lavalink_session_id, "channelId": str(channel_id), "guildId": str(guild_id)}}
        )
        return result
    except Exception as e:
        logger.error(f"❌ Lavalink join error: {e}")
        return False

async def lavalink_play(guild_id, track_url):
    if not lavalink_session_id:
        return False
    try:
        encoded = urllib.parse.quote(track_url, safe='')
        load = await lavalink_request(f"loadtracks?identifier={encoded}")
        if not load or 'tracks' not in load or not load['tracks']:
            logger.error(f"❌ No tracks loaded for: {track_url}")
            return False
        track_id = load['tracks'][0].get('encoded') or load['tracks'][0].get('track')
        if not track_id:
            return False
        return await lavalink_request(
            f"sessions/{lavalink_session_id}/players/{guild_id}",
            'PATCH',
            {"track": {"encoded": track_id}}
        )
    except Exception as e:
        logger.error(f"❌ Lavalink play error: {e}")
        return False

async def lavalink_stop(guild_id):
    if not lavalink_session_id:
        return False
    try:
        return await lavalink_request(
            f"sessions/{lavalink_session_id}/players/{guild_id}",
            'PATCH',
            {"track": {"encoded": None}}
        )
    except Exception as e:
        logger.error(f"❌ Lavalink stop error: {e}")
        return False

async def lavalink_pause(guild_id, pause=True):
    if not lavalink_session_id:
        return False
    try:
        return await lavalink_request(
            f"sessions/{lavalink_session_id}/players/{guild_id}",
            'PATCH',
            {"paused": pause}
        )
    except Exception as e:
        logger.error(f"❌ Lavalink pause error: {e}")
        return False

async def lavalink_leave(guild_id):
    if not lavalink_session_id:
        return False
    try:
        return await lavalink_request(f"sessions/{lavalink_session_id}/players/{guild_id}", 'DELETE')
    except Exception as e:
        logger.error(f"❌ Lavalink leave error: {e}")
        return False

# ==================== SEARCH ====================

async def search_youtube(query, requester):
    try:
        youtube_regex = r'(https?://)?(www\.)?(youtube|youtu|youtube-nocookie)\.(com|be)/'
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            if re.match(youtube_regex, query):
                info = await asyncio.to_thread(ydl.extract_info, query, False)
            else:
                info = await asyncio.to_thread(ydl.extract_info, f"ytsearch3:{query}", False)
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
                            'requester': requester,
                            'source': 'youtube'
                        })
            else:
                tracks.append({
                    'title': info.get('title', 'Unknown'),
                    'url': info.get('webpage_url', info.get('url')),
                    'duration': info.get('duration', 0),
                    'thumbnail': info.get('thumbnail', ''),
                    'requester': requester,
                    'source': 'youtube'
                })
            return tracks
    except Exception as e:
        logger.error(f"YouTube error: {e}")
        return []

async def search_spotify(query, requester):
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
                results = await asyncio.to_thread(sp.playlist_tracks, playlist_id)
                for item in results['items']:
                    track = item['track']
                    if track:
                        yt = await search_youtube(f"{track['name']} {track['artists'][0]['name']}", requester)
                        if yt:
                            yt[0]['source'] = 'spotify'
                            yt[0]['spotify_artist'] = track['artists'][0]['name']
                            tracks.append(yt[0])
            elif "track" in query:
                track_id = query.split("track/")[1].split("?")[0]
                result = await asyncio.to_thread(sp.track, track_id)
                yt = await search_youtube(f"{result['name']} {result['artists'][0]['name']}", requester)
                if yt:
                    yt[0]['source'] = 'spotify'
                    yt[0]['spotify_artist'] = result['artists'][0]['name']
                    tracks.append(yt[0])
        else:
            results = await asyncio.to_thread(sp.search, q=query, type='track', limit=5)
            for item in results['tracks']['items']:
                yt = await search_youtube(f"{item['name']} {item['artists'][0]['name']}", requester)
                if yt:
                    yt[0]['source'] = 'spotify'
                    yt[0]['spotify_artist'] = item['artists'][0]['name']
                    tracks.append(yt[0])
        return tracks
    except Exception as e:
        logger.error(f"Spotify error: {e}")
        return []

async def play_next(ctx, guild_id):
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
        if ctx.author and ctx.author.voice:
            await lavalink_join(guild_id, ctx.author.voice.channel.id)
        success = await lavalink_play(guild_id, next_track['url'])
        if success:
            embed = discord.Embed(title="🎵 Now Playing", description=f"**{next_track['title']}**", color=discord.Color.blue())
            if next_track.get('thumbnail'):
                embed.set_thumbnail(url=next_track['thumbnail'])
            if next_track.get('duration'):
                m = next_track['duration'] // 60
                s = next_track['duration'] % 60
                embed.add_field(name="Duration", value=f"{m}:{s:02d}", inline=True)
            if next_track.get('requester'):
                embed.add_field(name="Requested By", value=next_track['requester'], inline=True)
            await ctx.send(embed=embed)
        else:
            await ctx.send(f"❌ Could not play: {next_track['title']}")
            queue.is_playing = False
            await play_next(ctx, guild_id)
    except Exception as e:
        logger.error(f"Play error: {e}")
        queue.is_playing = False
        await play_next(ctx, guild_id)

# ==================== COMMANDS ====================

@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query):
    if not ctx.author.voice:
        await ctx.send("❌ You need to be in a voice channel!")
        return
    if not lavalink_connected:
        await ctx.send("❌ Lavalink not connected! Check !lavalink")
        return
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        music_queues[guild_id] = MusicQueue()
    queue = music_queues[guild_id]
    await ctx.send(f"🔍 Searching: {query}...")
    tracks = []
    if "spotify.com" in query:
        tracks = await search_spotify(query, ctx.author.mention)
    else:
        tracks = await search_youtube(query, ctx.author.mention)
    if not tracks:
        await ctx.send("❌ No results found!")
        return
    for track in tracks:
        queue.add(track)
    await ctx.send(f"✅ Added: **{tracks[0]['title']}**" if len(tracks) == 1 else f"✅ Added {len(tracks)} tracks")
    if not queue.is_playing:
        await play_next(ctx, guild_id)

@bot.command(name="skip")
async def skip(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing playing!")
        return
    queue = music_queues[guild_id]
    if not queue.is_playing:
        await ctx.send("❌ Nothing playing!")
        return
    await lavalink_stop(guild_id)
    queue.is_playing = False
    await ctx.send("⏭️ Skipped!")

@bot.command(name="stop")
async def stop(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
        music_queues[guild_id].is_playing = False
    await lavalink_leave(guild_id)
    await ctx.send("⏹️ Stopped!")

@bot.command(name="pause")
async def pause(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing playing!")
        return
    if await lavalink_pause(guild_id, True):
        await ctx.send("⏸️ Paused!")
    else:
        await ctx.send("❌ Failed to pause!")

@bot.command(name="resume")
async def resume(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing playing!")
        return
    if await lavalink_pause(guild_id, False):
        await ctx.send("▶️ Resumed!")
    else:
        await ctx.send("❌ Failed to resume!")

@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("📭 Queue empty!")
        return
    queue = music_queues[guild_id]
    if queue.is_empty() and not queue.current:
        await ctx.send("📭 Queue empty!")
        return
    embed = discord.Embed(title="🎵 Queue", color=discord.Color.blue())
    if queue.current and queue.is_playing:
        embed.add_field(name="Currently Playing", value=f"**{queue.current['title']}**", inline=False)
    if not queue.is_empty():
        text = ""
        for i, t in enumerate(queue.queue[:10], 1):
            text += f"`{i}.` {t['title']}\n"
        embed.add_field(name=f"Up Next ({len(queue.queue)})", value=text[:1024], inline=False)
    await ctx.send(embed=embed)

@bot.command(name="loop")
async def loop(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing playing!")
        return
    queue = music_queues[guild_id]
    queue.loop = not queue.loop
    await ctx.send(f"🔁 Loop {'enabled' if queue.loop else 'disabled'}!")

@bot.command(name="nowplaying", aliases=["np"])
async def now_playing(ctx):
    guild_id = ctx.guild.id
    if guild_id not in music_queues:
        await ctx.send("❌ Nothing playing!")
        return
    queue = music_queues[guild_id]
    if not queue.current or not queue.is_playing:
        await ctx.send("❌ Nothing playing!")
        return
    track = queue.current
    embed = discord.Embed(title="🎵 Now Playing", description=f"**{track['title']}**", color=discord.Color.blue())
    if track.get('thumbnail'):
        embed.set_thumbnail(url=track['thumbnail'])
    if track.get('duration'):
        m = track['duration'] // 60
        s = track['duration'] % 60
        embed.add_field(name="Duration", value=f"{m}:{s:02d}", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="clearqueue", aliases=["cq"])
async def clear_queue(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
        await ctx.send("🗑️ Queue cleared!")

@bot.command(name="leave")
async def leave(ctx):
    guild_id = ctx.guild.id
    if guild_id in music_queues:
        music_queues[guild_id].clear()
        music_queues[guild_id].is_playing = False
    await lavalink_leave(guild_id)
    await ctx.send("👋 Left!")

@bot.command(name="lavalink")
async def check_lavalink(ctx):
    """Check Lavalink connection status"""
    if lavalink_connected:
        embed = discord.Embed(title="🎧 Lavalink", description="✅ Connected!", color=discord.Color.green())
        embed.add_field(name="URL", value=LAVALINK_URL, inline=False)
        embed.add_field(name="Session ID", value=lavalink_session_id or "None", inline=False)
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="🎧 Lavalink",
            description="❌ Not connected!",
            color=discord.Color.red()
        )
        tried = "\n".join(f"• {c}" for c in LAVALINK_CANDIDATES)
        embed.add_field(name="Candidates Tried", value=tried or "none", inline=False)
        embed.add_field(name="Troubleshooting",
            value="1. Open the Lavalink service's own Railway logs - is it actually running?\n"
                  "2. Settings -> Networking on the Lavalink service: is 'Public Networking' or "
                  "'Private Networking' enabled? Use the matching host below.\n"
                  "3. Same project, private network: set LAVALINK_HOST=<name>.railway.internal "
                  "(no port needed in env, defaults to 8080)\n"
                  "4. Public domain: set LAVALINK_HOST=<name>.up.railway.app and leave "
                  "LAVALINK_PORT empty (Railway edge only serves HTTPS, no custom ports)\n"
                  "5. Confirm LAVALINK_SERVER_PASSWORD matches the Lavalink server's application.yml\n"
                  "6. Tip: set LAVALINK_HOST to a comma-separated list to have the bot "
                  "auto-try multiple addresses on startup",
            inline=False)
        await ctx.send(embed=embed)

@bot.command(name="reconnectlavalink")
@commands.has_permissions(administrator=True)
async def reconnect_lavalink(ctx):
    """Manually retry the Lavalink connection (Admin only)"""
    global lavalink_connected
    await ctx.send("🔄 Reconnecting to Lavalink...")
    ok = await lavalink_init()
    if ok:
        await ctx.send("✅ Reconnected!")
    else:
        await ctx.send("❌ Still failed. Check !lavalink for troubleshooting steps.")

# ==================== AFK ====================

async def move_to_afk(member, afk_channel):
    try:
        await member.move_to(afk_channel)
        await asyncio.sleep(0.5)
        await member.edit(mute=True, deafen=True)
    except Exception as e:
        logger.error(f"AFK error: {e}")

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

# ==================== PRESENCE ====================

async def update_member_presence(member):
    try:
        status_map = {discord.Status.online: "online", discord.Status.idle: "idle", discord.Status.dnd: "dnd", discord.Status.offline: "offline"}
        status = status_map.get(member.status, "offline")
        activities = []
        for activity in member.activities:
            if activity.type == discord.ActivityType.playing:
                activities.append({"type": "game", "name": activity.name})
            elif activity.type == discord.ActivityType.listening and activity.name == "Spotify":
                activities.append({"type": "spotify", "song": getattr(activity, "title", "Unknown")})
        if activities:
            payload = {"discord_id": str(member.id), "username": member.name, "status": status, "activities": activities, "last_updated": datetime.now().isoformat()}
            # FIX: timeout added here too so a slow/unreachable API doesn't stall the sync loop
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                headers = {"Authorization": API_SECRET, "Content-Type": "application/json"}
                try:
                    async with session.post(API_ENDPOINT, json=payload, headers=headers) as resp:
                        pass
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.error(f"Presence API request failed: {e}")
    except Exception as e:
        logger.error(f"Presence error: {e}")

@tasks.loop(minutes=5)
async def sync_members():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    logger.info(f"🔄 Syncing {len(guild.members)} members...")
    for member in guild.members:
        if not member.bot:
            await update_member_presence(member)
            await asyncio.sleep(0.1)
    logger.info("✅ Sync complete!")

# ==================== EVENTS ====================

@bot.event
async def on_ready():
    global lavalink_connected

    logger.info(f"✅ {bot.user} is online!")
    logger.info(f"📊 Bot ID: {bot.user.id}")
    logger.info(f"🎵 Music Bot Ready!")
    logger.info(f"🎧 Lavalink URL: {LAVALINK_URL}")

    # Connect to Lavalink with retries (now fails fast per attempt thanks to the timeout)
    for attempt in range(5):
        logger.info(f"🔄 Connecting to Lavalink (attempt {attempt + 1}/5)...")
        if await lavalink_init():
            logger.info("✅ Lavalink connected successfully!")
            break
        else:
            logger.warning(f"⚠️ Attempt {attempt + 1} failed, retrying in 5 seconds...")
            await asyncio.sleep(5)

    if not lavalink_connected:
        logger.error("❌ Failed to connect to Lavalink after 5 attempts! Bot will still run, but music commands won't work until !reconnectlavalink succeeds.")

    guild = bot.get_guild(GUILD_ID)
    if guild:
        logger.info(f"📋 Connected to: {guild.name}")
        for member in guild.members:
            if not member.bot:
                await update_member_presence(member)
                await asyncio.sleep(0.1)
        sync_members.start()
        logger.info("✅ Auto-sync started!")

@bot.event
async def on_presence_update(before, after):
    if not after.bot:
        await update_member_presence(after)

# ==================== BASIC COMMANDS ====================

@bot.command(name="ping")
async def ping(ctx):
    await ctx.send(f"🏓 Pong! {round(bot.latency * 1000)}ms")

@bot.command(name="stats")
async def stats(ctx):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        await ctx.send("❌ No server")
        return
    total = len([m for m in guild.members if not m.bot])
    online = len([m for m in guild.members if m.status != discord.Status.offline and not m.bot])
    embed = discord.Embed(title="📊 Stats", color=discord.Color.blue())
    embed.add_field(name="Members", value=str(total), inline=True)
    embed.add_field(name="Online", value=str(online), inline=True)
    embed.add_field(name="Lavalink", value="✅ Connected" if lavalink_connected else "❌ Disconnected", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="syncnow")
@commands.has_permissions(administrator=True)
async def sync_now(ctx):
    await ctx.send("🔄 Syncing...")
    await sync_members()
    await ctx.send("✅ Done!")

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
        "!clearqueue / !cq": "Clear the music queue",
        "!leave": "Bot leaves the voice channel",
        "!lavalink": "Check Lavalink connection status",
        "!reconnectlavalink": "Retry the Lavalink connection (Admin only)",
        "!ping": "Check bot latency",
        "!stats": "Show bot statistics",
        "!syncnow": "Force manual member sync (Admin only)"
    }
    text = ""
    for cmd, desc in commands_list.items():
        text += f"**{cmd}** - {desc}\n"
    embed.add_field(name="📋 Commands", value=text, inline=False)
    embed.set_footer(text="🎶 Enjoy the music!")
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    logger.error(f"Error: {error}")
    await ctx.send(f"❌ Error: {str(error)}")

# ==================== RUN ====================
if __name__ == "__main__":
    if not TOKEN:
        print("❌ ERROR: DISCORD_BOT_TOKEN not set!")
        exit(1)
    print("🚀 Starting bot with Lavalink...")
    print(f"🎧 Lavalink: {LAVALINK_URL}")
    bot.run(TOKEN, reconnect=True)
