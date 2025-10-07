from collections.abc import Collection, Iterable
import itertools
import sys
from typing import Callable, NamedTuple, Optional

from dats import Game as DatGame
from igdb_playlists import RUMBLE_KEYWORD_IDS, Playlist, Game as IgdbGame, ReleaseDate
from hasheous import DataObject

class PlaylistData(NamedTuple):
    playlist: Playlist
    igdb: Collection[IgdbGame]
    dats: Collection[DatGame]
    hasheous: Collection[DataObject]

class MatchRecord(NamedTuple):
    """
    A record of an attempt to match a game listed in one of this repo's DAT files
    with an entry in IGDB and/or Hasheous.
    Intended for output to a CSV file for later analysis.
    """

    name: str
    """
    The name of the game as listed in the DAT file.
    If the game is listed under multiple names,
    the first one found wins.
    """

    crc: Optional[str]
    """
    The CRC32 of the game's ROM, if available.
    """

    md5: Optional[str]
    """
    The MD5 hash of the game's ROM, if available.
    """

    sha1: Optional[str]
    """
    The SHA-1 hash of the game's ROM, if available.
    """

    serial: Optional[str]
    """
    The serial number of the game's ROM, if available.
    """

    hasheous_id: Optional[int]
    """
    The ID number of this game's entry on Hasheous, if one was found.
    """

    hasheous_url: Optional[str]
    """
    The URL of this game's entry on Hasheous, if one was found.
    """

    igdb_id: Optional[int]
    """
    The ID number of this game's entry on IGDB, if one was found.
    """

    igdb_url: Optional[str]
    """
    The URL of this game's entry on IGDB, if one was found.
    """

    igdb_release_id: Optional[int]
    """
    The ID number of this game's release on IGDB for the platform named by igdb_platform_id.
    """

    igdb_platform_id: Optional[int]
    """
    The ID number of the platform on IGDB that this game was released for.
    """


    @property
    def matched(self) -> bool:
        return \
            self.igdb_id is not None and \
            self.hasheous_id is not None and \
            (self.crc is not None or self.serial is not None)

class GameMatch(NamedTuple):
    source_dat: DatGame
    igdb: Optional[IgdbGame]
    hasheous: Optional[DataObject]
    generated_dat: Optional[DatGame]
    record: MatchRecord

def find[T](items: Iterable[T] | None, predicate: Callable[[T], bool]) -> T | None:
    """Find the first item in items that matches the predicate, or None if not found."""
    if items is None:
        return None

    for item in items:
        if predicate(item):
            return item

    return None


def match_games(playlist: PlaylistData) -> Iterable[GameMatch]:

    def generate_game(dat: DatGame, igdb: IgdbGame, hasheous: DataObject) -> DatGame:
        """Generate a new DatGame object by combining data from the given DatGame, IgdbGame, and Hasheous DataObject."""

        # TODO: Get the IGDB platform ID for dat
        # (can't just use the PlaylistData, as some playlists contain games for multiple platforms)
        def get_achievements():
            if hasheous.Metadata:
                for m in hasheous.Metadata:
                    if m.Source == 'RetroAchievements' and m.Status == 'Mapped':
                        return True

            return None

        def get_cero():
            cero = find(igdb.age_ratings, lambda r: r.organization.name == "CERO")
            return cero.rating_category.rating if cero else None

        def get_coop():
            if igdb.multiplayer_modes:
                for m in igdb.multiplayer_modes:
                    if m.coop:
                        # TODO: Only return true if the coop mode is for the current platform
                        return True

            if igdb.game_modes:
                for m in igdb.game_modes:
                    if m.id == 3: # IGDB ID for "Co-operative"
                        # TODO: Only return true if the coop mode is for the current platform
                        return True

            # Can't definitively say there's no coop mode, so return None
            return None

        def get_date(release: ReleaseDate | None):
            if release and release.human:
                return release.human

            # TODO: How to handle cancelled games?
            # TODO: Format the date as YYYY-MM-DD
            return dat.date

        def get_developer():
            if not igdb.involved_companies:
                return None

            return '|'.join(c.company.name for c in igdb.involved_companies if c.developer or c.porting)

        def get_esrb():
            esrb = find(igdb.age_ratings, lambda r: r.organization.name == "ESRB")
            return esrb.rating_category.rating if esrb else None

        def get_franchise():
            return igdb.franchise.name if igdb.franchise else None
            # TODO: Handle multiple franchises

        def get_genre():
            return '|'.join(g.name.title() for g in igdb.genres) if igdb.genres else None
            # Some string fields in RetroArch are treated as lists delimited by pipes, commas, or slashes.

        def get_pegi():
            pegi = find(igdb.age_ratings, lambda r: r.organization.name == "PEGI")
            return pegi.rating_category.rating if pegi else None

        def get_perspective():
            perspective = None
            if igdb.player_perspectives:
                perspective = '|'.join(p.name.title() for p in igdb.player_perspectives)
            return perspective

        def get_platform_exclusive():
            if not igdb.platforms:
                return None

            num_platforms = len(igdb.platforms)
            num_remakes = len(igdb.remakes or ()) # TODO: Only count remakes on different platforms
            num_ports = len(igdb.ports or ()) # TODO: Only count ports on different platforms
            num_remasters = len(igdb.remasters or ()) # TODO: Only count remasters on different platforms
            num_collections = len(igdb.collections or ()) # TODO: Only count collections on different platforms
            total_releases = num_platforms + num_remakes + num_ports + num_remasters + num_collections

            return total_releases == 1

        def get_publisher():
            publisher = None
            if igdb.involved_companies:
                publisher = '|'.join(c.company.name for c in igdb.involved_companies if c.publisher)
            return publisher

        def get_region():
            # TODO: Guess the region from the DAT's name if the region isn't given
            # TODO: Guess the region from matching release dates if the region isn't given
            return dat.region

        def get_release(region: str | None):
            if not igdb.release_dates:
                return None

            if len(igdb.release_dates) == 1:
                return igdb.release_dates[0]

            if not region:
                return None

            region_lower = region.lower()
            return find(igdb.release_dates, lambda rd: rd.release_region.region.lower() == region_lower)

        def get_rumble():
            rumble_keyword = find(igdb.keywords, lambda k: k.id in RUMBLE_KEYWORD_IDS)
            if rumble_keyword:
                return True
            elif dat.rumble is not None:
                return dat.rumble
            return None

        def get_tags():
            keywords = (k.name.title() for k in igdb.keywords) if igdb.keywords else ()
            themes = (t.name.title() for t in igdb.themes) if igdb.themes else ()
            tags = sorted(itertools.chain(keywords, themes))
            return '|'.join(tags) if tags else None

        def get_serial():
            if dat.serial:
                return dat.serial
            elif serial_rom := find(dat.rom, lambda r: r.serial is not None):
                return serial_rom.serial
            else:
                return None

        def get_users():
            if not igdb.multiplayer_modes:
                return None

            users = 1
            for m in igdb.multiplayer_modes:
                users = max(users, m.offlinecoopmax or 0, m.offlinemax or 0, m.onlinecoopmax or 0, m.onlinemax or 0)

            return users

        def get_year(release: ReleaseDate | None):
            if release and release.y:
                return release.y

            # TODO: How to handle cancelled games?

            return dat.year

        region = get_region()
        release = get_release(region)


        return DatGame(
            name=dat.name_key,
            rom=dat.rom,
            achievements=get_achievements(),
            #analog
            #artstyle
            #bbfc_rating
            #category
            cero_rating=get_cero(),
            #code
            #console_exclusive
            #controls
            coop=get_coop(),
            date=get_date(release),
            developer=get_developer(),
            #download
            #edge_issue
            #edge_rating
            #elspa_rating
            #enhancement_hardware
            #enhancement_hw
            esrb_rating=get_esrb(),
            #famitsu_rating
            franchise=get_franchise(),
            #gameplay
            genre=get_genre(),
            #homepage
            igdb_id=igdb.id,
            #igdb_platform_id
            #igdb_release_date_id
            #language
            #license
            #manufacturer
            #media
            #narrative
            #origin
            #pacing
            #patch
            pegi_rating=get_pegi(),
            perspective=get_perspective(),
            platform_exclusive=get_platform_exclusive(),
            publisher=get_publisher(),
            region=region,
            #releaseday
            releasemonth=release.m if release else None,
            releaseyear=release.y if release else None,
            rumble=get_rumble(),
            #score
            serial=get_serial(),
            #setting
            tags=get_tags(),
            users=get_users(),
            #vehicular
            #version
            #visual
            year=get_year(release)

        )

    for dat in playlist.dats:
        if not dat.rom or len(dat.rom) == 0:
            print(f"Warning: DAT game '{dat.name_key}' has no ROMs, skipping", file=sys.stderr)
            continue

        rom = dat.rom[0]
        crc = rom.crc
        md5 = rom.md5
        serial = rom.serial
        sha1 = rom.sha1

        hasheous_entry = find(playlist.hasheous, lambda d: d.has_rom(crc, md5, sha1))

        igdb_id = hasheous_entry.igdb_id if hasheous_entry else None
        igdb_entry = find(playlist.igdb, lambda g: g.id == igdb_id)

        game = generate_game(dat, igdb_entry, hasheous_entry) if (igdb_entry and hasheous_entry) else None
        match_record = MatchRecord(
            name=dat.name_key,
            crc=crc,
            md5=md5,
            sha1=sha1,
            serial=serial,
            igdb_id=igdb_entry.id if igdb_entry else None,
            igdb_url=igdb_entry.url if igdb_entry else None,
            igdb_release_id=None, # TODO: Populate this field
            igdb_platform_id=None, # TODO: Populate this field
            hasheous_id=hasheous_entry.Id if hasheous_entry else None,
            hasheous_url=None # TODO: Populate this field
        )
        yield GameMatch(
            source_dat=dat,
            igdb=igdb_entry,
            hasheous=hasheous_entry,
            generated_dat=game,
            record=match_record,
        )

__all__ = ("PlaylistData", "match_games", "GameMatch", "MatchRecord")