import discord
import os
import asyncio
import aiohttp
import wavelink
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

# Lavalink
LAVALINK_HOST = os.environ.get('LAVALINK_HOST', '')
LAVALINK_PORT = os.environ.get('LAVALINK_PORT', '443')
LAVALINK_PASSWORD = os.environ.get('LAVALINK_PASSWORD', '')
LAVALINK_SSL = os.environ.get('LAVALINK_SSL', 'true').lower() in ('1', 'true', 'yes')

# Spotify API (used only to resolve metadata; playback still goes through Lavalink)
SPOTIFY_CLIENT_ID = os.environ.get('SPOTIFY_CLIENT_ID', '')
SPOTIFY_CLIENT_SECRET = os.environ.get('SPOTIFY_CLIENT_SECRET', '')

# ==================== BOT SETUP ====================
intents = discord.Intents.default()
intents.presences = True
intents.members = True
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


class Player(wavelink.Player):
    """wavelink Player subclass so we can remember which text channel to post updates in."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.home: discord.abc.Messageable | None = None


# ==================== LAVALINK CONNECTION ====================

async def connect_lavalink(max_attempts: int = 5, base_delay: float = 3.0):
    if not LAVALINK_HOST or not LAVALINK_PASSWORD:
        logger.error("❌ LAVALINK_HOST or LAVALINK_PASSWORD not set — skipping Lavalink connection.")
        return
    scheme = "https" if LAVALINK_SSL else "http"
    uri = f"{scheme}://{LAVALINK_HOST}:{LAVALINK_PORT}"

    for attempt in range(1, max_attempts + 1):
        node = wavelink.Node(uri=uri, password=LAVALINK_PASSWORD)
        try:
            await wavelink.Pool.connect(nodes=[node], client=bot)
            logger.info(f"✅ Connected to Lavalink node at {uri} (attempt {attempt})")
            return
        except Exception as e:
            logger.error(f"❌ Lavalink connect attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                delay = base_delay * attempt
                logger.info(f"⏳ Retrying Lavalink connection in {delay:.0f}s...")
                await asyncio.sleep(delay)

    logger.error("❌ Exhausted all Lavalink connection attempts. Music commands will fail until the bot restarts or /reconnectlavalink is run.")


@bot.command(name="reconnectlavalink")
@commands.has_permissions(administrator=True)
async def reconnect_lavalink(ctx):
    """Manually retry the Lavalink node connection (Admin only)"""
    await ctx.send("🔄 Retrying Lavalink connection...")
    await connect_lavalink()
    if wavelink.Pool.nodes:
        await ctx.send("✅ Lavalink connected!")
    else:
        await ctx.send("❌ Still couldn't connect — check the Lavalink service is running and the password matches.")


@bot.event
async def on_wavelink_node_ready(payload: wavelink.NodeReadyEventPayload):
    logger.info(f"✅ Wavelink node ready: {payload.node.uri} (resumed={payload.resumed})")


@bot.event
async def on_wavelink_track_start(payload: wavelink.TrackStartEventPayload):
    player: Player = payload.player  # type: ignore
    track = payload.track
    if not player or not player.home:
        return

    embed = discord.Embed(
        title="🎵 Now Playing",
        description=f"**{track.title}**",
        color=discord.Color.blue()
    )
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    if track.length:
        minutes, seconds = divmod(track.length // 1000, 60)
        embed.add_field(name="Duration", value=f"{minutes}:{seconds:02d}", inline=True)

    requester = getattr(track.extras, "requester", None) if track.extras else None
    if requester:
        embed.add_field(name="Requested By", value=requester, inline=True)

    spotify_artist = getattr(track.extras, "spotify_artist", None) if track.extras else None
    if spotify_artist:
        embed.add_field(name="🎵 Spotify Artist", value=spotify_artist, inline=True)

    await player.home.send(embed=embed)


@bot.event
async def on_wavelink_inactive_player(player: Player):
    if player.home:
        await player.home.send("📭 Queue is empty — leaving the voice channel due to inactivity.")
    await player.disconnect()


# ==================== VOICE CONNECTION ====================

async def connect_voice(ctx) -> tuple[Player | None, str | None]:
    """Connect to voice channel, returning a wavelink Player."""
    if not ctx.author.voice:
        return None, "❌ You need to be in a voice channel!"

    voice_channel = ctx.author.voice.channel

    player: Player = ctx.voice_client  # type: ignore
    if player:
        if player.channel == voice_channel:
            player.home = ctx.channel
            return player, None
        await player.disconnect()
        await asyncio.sleep(1)

    try:
        player = await voice_channel.connect(cls=Player, timeout=30.0)
        player.home = ctx.channel
        player.autoplay = wavelink.AutoPlayMode.partial
        logger.info(f"✅ Connected to {voice_channel.name}")
        return player, None
    except Exception as e:
        logger.error(f"Voice connect failed: {e}")
        return None, f"❌ Failed to connect: {str(e)}"


# ==================== SPOTIFY METADATA RESOLUTION ====================

SPOTIFY_PLAYLIST_TRACK_LIMIT = 100  # safety cap so a huge playlist doesn't hang !play


SPOTIFY_REFRESH_TOKEN = os.environ.get('SPOTIFY_REFRESH_TOKEN', '')

_spotify_client = None


def get_spotify_client():
    """Return a cached Spotify client. Uses a stored user refresh token (access to
    private/collaborative playlists that user can see) if SPOTIFY_REFRESH_TOKEN is
    set, otherwise falls back to Client Credentials (public data only)."""
    global _spotify_client
    if _spotify_client is not None:
        return _spotify_client

    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

    if SPOTIFY_REFRESH_TOKEN:
        auth_manager = SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri="http://127.0.0.1:8888/callback",  # unused for refresh, but required by spotipy
            scope="playlist-read-private playlist-read-collaborative",
        )
        # Seed the cache with the stored refresh token so spotipy can mint fresh
        # access tokens without ever needing an interactive browser login here.
        token_info = auth_manager.refresh_access_token(SPOTIFY_REFRESH_TOKEN)
        auth_manager.cache_handler.save_token_to_cache(token_info)
        _spotify_client = spotipy.Spotify(auth_manager=auth_manager)
    else:
        _spotify_client = spotipy.Spotify(auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET
        ))
    return _spotify_client


async def resolve_spotify_tracks(query):
    """Resolve a Spotify track/playlist/album URL into a list of (search_query, artist) tuples."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None

    try:
        sp = get_spotify_client()

        if "track" in query:
            track_id = query.split("track/")[1].split("?")[0]
            result = sp.track(track_id)
            artist = result['artists'][0]['name']
            return [(f"{result['name']} {artist}", artist)]

        if "playlist" in query:
            playlist_id = query.split("playlist/")[1].split("?")[0]
            tracks = []
            try:
                results = sp.playlist_items(playlist_id, additional_types=["track"])
            except Exception as e:
                logger.error(f"Spotify playlist access error (likely requires user auth, not just Client Credentials): {e}")
                return "AUTH_REQUIRED"
            while results:
                for item in results.get('items', []):
                    t = item.get('track')
                    if t and t.get('name') and t.get('artists'):
                        artist = t['artists'][0]['name']
                        tracks.append((f"{t['name']} {artist}", artist))
                        if len(tracks) >= SPOTIFY_PLAYLIST_TRACK_LIMIT:
                            return tracks
                results = sp.next(results) if results.get('next') else None
            return tracks or None

        if "album" in query:
            album_id = query.split("album/")[1].split("?")[0]
            tracks = []
            try:
                results = sp.album_tracks(album_id)
            except Exception as e:
                logger.error(f"Spotify album access error (likely requires user auth, not just Client Credentials): {e}")
                return "AUTH_REQUIRED"
            while results:
                for t in results.get('items', []):
                    if t.get('name') and t.get('artists'):
                        artist = t['artists'][0]['name']
                        tracks.append((f"{t['name']} {artist}", artist))
                        if len(tracks) >= SPOTIFY_PLAYLIST_TRACK_LIMIT:
                            return tracks
                results = sp.next(results) if results.get('next') else None
            return tracks or None

        return None
    except Exception as e:
        logger.error(f"Spotify resolve error: {e}")
        return None


# ==================== MUSIC COMMANDS ====================

DEFAULT_SEARCH_SOURCE = os.environ.get('DEFAULT_SEARCH_SOURCE', 'soundcloud')  # 'soundcloud' or 'youtube_music' or 'youtube'

_SOURCE_MAP = {
    'soundcloud': wavelink.TrackSource.SoundCloud,
    'youtube_music': wavelink.TrackSource.YouTubeMusic,
    'youtube': wavelink.TrackSource.YouTube,
}
_DEFAULT_SOURCE = _SOURCE_MAP.get(DEFAULT_SEARCH_SOURCE, wavelink.TrackSource.SoundCloud)


async def search_playable(text: str) -> wavelink.Search:
    """Search Lavalink for a track/playlist. URLs pass through untouched;
    plain text is searched against the configured default source (SoundCloud
    unless overridden), using wavelink's actual source= parameter rather than
    a manually embedded string prefix — embedding a prefix in the query text
    gets double-prefixed by wavelink's own default and silently breaks."""
    if text.startswith("http://") or text.startswith("https://"):
        return await wavelink.Playable.search(text)
    return await wavelink.Playable.search(text, source=_DEFAULT_SOURCE)


@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query):
    """Play a song from YouTube or Spotify via Lavalink"""
    player, error = await connect_voice(ctx)
    if error:
        await ctx.send(error)
        return

    await ctx.send(f"🔍 Searching for: {query}...")

    if "spotify.com" in query:
        resolved = await resolve_spotify_tracks(query)

        if resolved == "AUTH_REQUIRED":
            await ctx.send(
                "❌ Spotify blocked this one — it's likely **private, collaborative, or a Spotify-generated "
                "playlist** (Blend, Discover Weekly, algorithmic Mix). Those specifically require the owner's "
                "login and no bot can bypass that restriction. **Genuinely public playlists work fine** — "
                "try a different link, or paste individual track URLs instead."
            )
            return

        if not resolved:
            await ctx.send("❌ Couldn't resolve that Spotify link! (Track links are the most reliable.)")
            return

        added = 0
        for search_query, artist in resolved:
            try:
                result: wavelink.Search = await search_playable(search_query)
            except Exception as e:
                logger.error(f"Lavalink search error for '{search_query}': {e}")
                continue

            if not result:
                continue

            track = result.tracks[0] if isinstance(result, wavelink.Playlist) else result[0]
            if not track:
                continue

            track.extras = {"requester": ctx.author.mention, "spotify_artist": artist}
            await player.queue.put_wait(track)
            added += 1

        if added == 0:
            await ctx.send("❌ Couldn't find playable matches for that Spotify link!")
            return

        await ctx.send(f"✅ Added {added} track(s) from Spotify to queue")

        if not player.playing:
            await player.play(player.queue.get())
        return

    try:
        result: wavelink.Search = await search_playable(query)
    except Exception as e:
        logger.error(f"Lavalink search error: {e}")
        await ctx.send("❌ Search failed — the Lavalink node may not have a working source plugin.")
        return

    if not result:
        await ctx.send("❌ No results found! Please try a different song.")
        return

    if isinstance(result, wavelink.Playlist):
        for track in result.tracks:
            track.extras = {"requester": ctx.author.mention}
        await player.queue.put_wait(result)
        await ctx.send(f"✅ Added playlist **{result.name}** ({len(result.tracks)} tracks) to queue")
    else:
        track = result[0]
        track.extras = {"requester": ctx.author.mention}
        await player.queue.put_wait(track)
        await ctx.send(f"✅ Added to queue: **{track.title}**")

    if not player.playing:
        await player.play(player.queue.get())


@bot.command(name="skip")
async def skip(ctx):
    """Skip the current song"""
    player: Player = ctx.voice_client  # type: ignore
    if not player or not player.playing:
        await ctx.send("❌ Nothing is playing!")
        return
    await player.skip(force=True)
    await ctx.send("⏭️ Skipped the current song!")


@bot.command(name="stop")
async def stop(ctx):
    """Stop playback and clear the queue"""
    player: Player = ctx.voice_client  # type: ignore
    if not player:
        await ctx.send("❌ I'm not in a voice channel!")
        return
    player.queue.clear()
    await player.stop()
    await player.disconnect()
    await ctx.send("⏹️ Stopped playback and cleared queue!")


@bot.command(name="pause")
async def pause(ctx):
    """Pause the current song"""
    player: Player = ctx.voice_client  # type: ignore
    if player and player.playing and not player.paused:
        await player.pause(True)
        await ctx.send("⏸️ Paused the current song!")
    else:
        await ctx.send("❌ Nothing is playing!")


@bot.command(name="resume")
async def resume(ctx):
    """Resume the current song"""
    player: Player = ctx.voice_client  # type: ignore
    if player and player.paused:
        await player.pause(False)
        await ctx.send("▶️ Resumed playback!")
    else:
        await ctx.send("❌ Nothing is paused!")


@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx):
    """Show the current music queue"""
    player: Player = ctx.voice_client  # type: ignore
    if not player or (not player.current and player.queue.is_empty):
        await ctx.send("📭 Queue is empty!")
        return

    embed = discord.Embed(title="🎵 Music Queue", color=discord.Color.blue())
    if player.current:
        embed.add_field(name="🎶 Currently Playing", value=f"**{player.current.title}**", inline=False)

    if not player.queue.is_empty:
        queue_text = ""
        for i, track in enumerate(list(player.queue)[:10], 1):
            queue_text += f"`{i}.` {track.title}\n"
        embed.add_field(name=f"⏭️ Up Next ({len(player.queue)} tracks)", value=queue_text[:1024], inline=False)

    embed.set_footer(text=f"Queue size: {len(player.queue)}")
    await ctx.send(embed=embed)


@bot.command(name="loop")
async def loop(ctx):
    """Toggle loop for the current song"""
    player: Player = ctx.voice_client  # type: ignore
    if not player:
        await ctx.send("❌ Nothing is playing!")
        return
    if player.queue.mode == wavelink.QueueMode.loop:
        player.queue.mode = wavelink.QueueMode.normal
        await ctx.send("🔁 Loop disabled!")
    else:
        player.queue.mode = wavelink.QueueMode.loop
        await ctx.send("🔁 Loop enabled!")


@bot.command(name="nowplaying", aliases=["np"])
async def now_playing(ctx):
    """Show the currently playing song"""
    player: Player = ctx.voice_client  # type: ignore
    if not player or not player.current:
        await ctx.send("❌ Nothing is playing!")
        return

    track = player.current
    embed = discord.Embed(title="🎵 Now Playing", description=f"**{track.title}**", color=discord.Color.blue())
    if track.artwork:
        embed.set_thumbnail(url=track.artwork)
    if track.length:
        minutes, seconds = divmod(track.length // 1000, 60)
        embed.add_field(name="⏱️ Duration", value=f"{minutes}:{seconds:02d}", inline=True)
    if track.author:
        embed.add_field(name="👤 Uploader", value=track.author, inline=True)

    requester = getattr(track.extras, "requester", None) if track.extras else None
    if requester:
        embed.add_field(name="📝 Requested By", value=requester, inline=True)

    spotify_artist = getattr(track.extras, "spotify_artist", None) if track.extras else None
    if spotify_artist:
        embed.add_field(name="🎵 Spotify Artist", value=spotify_artist, inline=True)

    if player.queue.mode == wavelink.QueueMode.loop:
        embed.add_field(name="🔁 Loop", value="Enabled", inline=True)

    await ctx.send(embed=embed)


@bot.command(name="clearqueue", aliases=["cq"])
async def clear_queue(ctx):
    """Clear the music queue"""
    player: Player = ctx.voice_client  # type: ignore
    if player and not player.queue.is_empty:
        player.queue.clear()
        await ctx.send("🗑️ Queue cleared!")
    else:
        await ctx.send("📭 Queue is already empty!")


@bot.command(name="remove")
async def remove_from_queue(ctx, position: int):
    """Remove a song from the queue by position"""
    player: Player = ctx.voice_client  # type: ignore
    if not player or player.queue.is_empty:
        await ctx.send("📭 Queue is empty!")
        return
    try:
        removed = player.queue.delete(position - 1)
        await ctx.send(f"✅ Removed: **{removed.title}**")
    except Exception:
        await ctx.send(f"❌ No track at position {position}")


@bot.command(name="shuffle")
async def shuffle_queue(ctx):
    """Shuffle the music queue"""
    player: Player = ctx.voice_client  # type: ignore
    if not player or len(player.queue) < 2:
        await ctx.send("❌ Need at least 2 songs to shuffle!")
        return
    player.queue.shuffle()
    await ctx.send("🔀 Queue shuffled!")


@bot.command(name="leave")
async def leave(ctx):
    """Make the bot leave the voice channel"""
    player: Player = ctx.voice_client  # type: ignore
    if player:
        player.queue.clear()
        await player.disconnect()
        await ctx.send("👋 Left the voice channel!")
    else:
        await ctx.send("❌ I'm not in a voice channel!")


# ==================== AFK FUNCTIONS ====================

voice_activity = {}

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
    elif after.channel and before.channel and after.channel.id == before.channel.id:
        # Same channel, but something changed — self-mute/deafen/video/stream toggles are
        # user-initiated actions, so treat them as activity and reset the timer.
        state_changed = (
            before.self_mute != after.self_mute
            or before.self_deaf != after.self_deaf
            or before.self_video != after.self_video
            or before.self_stream != after.self_stream
        )
        if state_changed and member.id in voice_activity:
            voice_activity[member.id]["last_active"] = datetime.now()

    if after.channel and after.channel.id != AFK_CHANNEL_ID:
        asyncio.create_task(check_afk(member))


def bot_playing_in_channel(channel_id: int) -> bool:
    """True if a wavelink Player is actively playing audio in the given voice channel."""
    if not wavelink.Pool.nodes:
        return False
    for node in wavelink.Pool.nodes.values():
        for player in node.players.values():
            if player.channel and player.channel.id == channel_id and player.playing and not player.paused:
                return True
    return False


async def check_afk(member):
    if not AFK_CHANNEL_ID:
        return
    await asyncio.sleep(AFK_TIMEOUT_MINUTES * 60)
    if not member.voice or not member.voice.channel or member.voice.channel.id == AFK_CHANNEL_ID:
        return
    if bot_playing_in_channel(member.voice.channel.id):
        # Music is actively playing in this channel — don't AFK anyone listening to it.
        # Re-check again after another timeout window instead of giving up entirely.
        asyncio.create_task(check_afk(member))
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
    logger.info(f"✅ {bot.user} is online!")
    logger.info(f"📊 Bot ID: {bot.user.id}")
    logger.info(f"🎵 Music Bot Ready (Lavalink mode)!")

    await connect_lavalink()

    guild = bot.get_guild(GUILD_ID)
    if guild:
        logger.info(f"📋 Connected to server: {guild.name}")
        logger.info(f"👥 Members: {len(guild.members)}")

        # Back-fill AFK tracking for anyone already in a voice channel — without this,
        # every redeploy silently drops AFK tracking for already-connected members
        # until they manually leave and rejoin.
        if AFK_CHANNEL_ID:
            backfilled = 0
            for vc in guild.voice_channels:
                if vc.id == AFK_CHANNEL_ID:
                    continue
                for member in vc.members:
                    if not member.bot:
                        voice_activity[member.id] = {"channel_id": vc.id, "last_active": datetime.now()}
                        asyncio.create_task(check_afk(member))
                        backfilled += 1
            if backfilled:
                logger.info(f"🎙️ Backfilled AFK tracking for {backfilled} already-connected member(s)")

        logger.info("🔄 Running initial member sync...")
        for member in guild.members:
            if not member.bot:
                await update_member_presence(member)
                await asyncio.sleep(0.1)

        logger.info("✅ Initial sync complete!")

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

    total_queued = sum(len(p.queue) for p in wavelink.Pool.get_node().players.values()) if wavelink.Pool.nodes else 0
    embed.add_field(name="🎵 Total Queued", value=str(total_queued), inline=True)

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
        "!ping": "Check bot latency",
        "!stats": "Show bot statistics",
        "!syncnow": "Force manual member sync (Admin only)",
        "!help": "Show this help message"
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
        print("⚠️ WARNING: GUILD_ID not set!")
    if not LAVALINK_HOST or not LAVALINK_PASSWORD:
        print("⚠️ WARNING: LAVALINK_HOST / LAVALINK_PASSWORD not set — music commands will fail!")

    print("=" * 50)
    print("🚀 Starting bot (Lavalink mode)...")
    print("📡 Member tracking: Enabled (auto-sync every 5 minutes)")
    print(f"🎙️ AFK management: {'Enabled' if AFK_CHANNEL_ID else 'Disabled'}")
    print("=" * 50)
    bot.run(TOKEN, reconnect=True)
