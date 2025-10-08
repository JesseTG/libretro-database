from collections.abc import Collection, Iterable
import itertools
import sys
from typing import Callable, NamedTuple, Optional

from dats import Game as DatGame
from igdb_playlists import ANALOG_KEYWORD_IDS, RUMBLE_KEYWORD_IDS, Playlist, Game as IgdbGame, ReleaseDate
from hasheous import DataObject
from pycountry import countries

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

        # TODO: How to handle games with multiple ROMs (e.g. bin/cue games)?

        def get_achievements():
            if hasheous.Metadata:
                for m in hasheous.Metadata:
                    if m.Source == 'RetroAchievements' and m.Status == 'Mapped':
                        return True

            return None

        def get_analog():
            analog_keyword = find(igdb.keywords, lambda k: k.id in ANALOG_KEYWORD_IDS)
            if analog_keyword:
                return True
            elif dat.analog is not None:
                return dat.analog
            return None

        def get_cero(release: ReleaseDate | None):
            region = release.release_region.id if release else None
            if region not in (5, 7, 8, None):
                # 5 = Japan
                # 7 = Asia
                # 8 = Worldwide
                # CERO is a Japanese rating system,
                # so omit it if this release is known to be somewhere else.
                return None

            cero = find(igdb.age_ratings, lambda r: r.organization.name == "CERO")
            return cero.rating_category.rating if cero else None

        def get_coop(release: ReleaseDate | None):
            if igdb.multiplayer_modes:
                for m in igdb.multiplayer_modes:
                    if m.coop and m.platform and release and m.platform.id == release.platform.id:
                        return True

            if igdb.game_modes:
                for m in igdb.game_modes:
                    if m.id == 3: # IGDB ID for "Co-operative"
                        return True

            # Can't definitively say there's no coop mode, so return None
            return None

        def get_developer():
            if not igdb.involved_companies:
                return None

            return '|'.join(c.company.name for c in igdb.involved_companies if c.developer or c.porting)

        def get_esrb(release: ReleaseDate | None):
            region = release.release_region.id if release else None
            if region not in (2, 8, None):
                # 2 = North America
                # 8 = Worldwide
                # ESRB is a North American rating system,
                # so omit it if this release is known to be outside North America.
                return None

            esrb = find(igdb.age_ratings, lambda r: r.organization.name == "ESRB")
            return esrb.rating_category.rating if esrb else None

        def get_franchise():
            return igdb.franchise.name if igdb.franchise else None
            # TODO: Handle multiple franchises

        def get_genre():
            return '|'.join(g.name.title() for g in igdb.genres) if igdb.genres else None
            # Some string fields in RetroArch are treated as lists delimited by pipes, commas, or slashes.

        def get_language():
            # TODO: Extract languages from the DAT's name (check for Goodtools/No-Intro/Redump/TOSEC conventions)
            if not igdb.language_supports:
                return None

            language_names: set[str] = set()
            language_supports = sorted((l for l in igdb.language_supports), key=lambda l: l.language.name)
            languages = itertools.groupby(language_supports, key=lambda l: l.language.name)
            for (lang, supports) in languages:
                language_names.add(lang)
                language_names.update(f"{s.language.name} ({s.language_support_type.name})" for s in supports)

            if not language_names:
                return None

            return '|'.join(sorted(language_names))

        def get_pegi(release: ReleaseDate | None):
            region = release.release_region.id if release else None
            if region not in (1, 8, None):
                # 1 = Europe
                # 8 = Worldwide
                # PEGI is a European rating system,
                # so omit it if this release is known to be outside Europe.
                return None

            pegi = find(igdb.age_ratings, lambda r: r.organization.name == "PEGI")
            return pegi.rating_category.rating if pegi else None

        def get_perspective():
            if not igdb.player_perspectives:
                return None

            return '|'.join(p.name.title() for p in igdb.player_perspectives)

        def get_platform_exclusive():
            # TODO: What to do about legacy re-releases?
            if not igdb.platforms:
                return None

            num_platforms = len(igdb.platforms)
            num_remakes = len(igdb.remakes or ()) # TODO: Only count remakes on different platforms
            num_ports = len(igdb.ports or ()) # TODO: Only count ports on different platforms
            num_remasters = len(igdb.remasters or ()) # TODO: Only count remasters on different platforms
            num_collections = len(igdb.collections or ()) # TODO: Only count collections on different platforms
            total_releases = num_platforms + num_remakes + num_ports + num_remasters + num_collections

            return total_releases == 1

        def get_origin():
            if not igdb.involved_companies:
                return None

            country_codes = {c.company.country for c in igdb.involved_companies if c.developer and c.company.country}
            country_objects = (countries.get(numeric=str(c)) for c in country_codes)
            country_names: tuple[str, ...] = tuple(c.name for c in country_objects if c)

            if not country_names:
                return None

            return '|'.join(sorted(country_names))

        def get_publisher():
            if not igdb.involved_companies:
                return None

            return '|'.join(c.company.name for c in igdb.involved_companies if c.publisher)

        def get_region():
            # TODO: Guess the region from the DAT's name if the region isn't given
            # TODO: Guess the region from matching release dates if the region isn't given
            return dat.region

        def get_release(region: str | None):
            # TODO: What to do about cancelled games?
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

        region = get_region()
        release = get_release(region)

        return DatGame(
            name=dat.name_key,
            rom=dat.rom,
            achievements=get_achievements(),
            analog=get_analog(),
            cero_rating=get_cero(release),
            #console_exclusive
            coop=get_coop(release),
            developer=get_developer(),
            #enhancement_hw
            esrb_rating=get_esrb(release),
            franchise=get_franchise(),
            genre=get_genre(),
            igdb_id=igdb.id,
            igdb_url=igdb.url,
            igdb_platform_id=release.platform.id if release else None,
            igdb_release_date_id=release.id if release else None,
            language=get_language(),
            origin=get_origin(),
            pegi_rating=get_pegi(release),
            perspective=get_perspective(),
            platform_exclusive=get_platform_exclusive(),
            publisher=get_publisher(),
            region=region,
            releasemonth=release.m if release else None,
            releaseyear=release.y if release else None,
            rumble=get_rumble(),
            serial=get_serial(),
            tags=get_tags(),
            users=get_users(),
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
            igdb_release_id=game.igdb_release_date_id if game else None,
            igdb_platform_id=game.igdb_platform_id if game else None,
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