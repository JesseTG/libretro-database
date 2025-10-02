from collections.abc import Collection, Iterable
import itertools
import sys
from typing import Callable, NamedTuple

from dats import Game as DatGame
from igdb_playlists import RUMBLE_KEYWORD_IDS, Playlist, Game as IgdbGame
from hasheous import DataObject

class PlaylistData(NamedTuple):
    playlist: Playlist
    igdb: Collection[IgdbGame]
    dats: Collection[DatGame]
    hasheous: Collection[DataObject]

def find[T](items: Iterable[T] | None, predicate: Callable[[T], bool]) -> T | None:
    """Find the first item in items that matches the predicate, or None if not found."""
    if items is None:
        return None

    for item in items:
        if predicate(item):
            return item

    return None


def generate_games(playlist: PlaylistData, verbose=False) -> Iterable[DatGame]:
    # TODO: Keep track of which IGDB objects we used, and which we didn't
    # TODO: Keep track of which Hasheous objects we used, and which we didn't
    # TODO: Keep track of which DAT games we used, and which we didn't

    def generate_game(dat: DatGame, igdb: IgdbGame, hasheous: DataObject) -> DatGame:
        """Generate a new DatGame object by combining data from the given DatGame, IgdbGame, and Hasheous DataObject."""

        def get_cero():
            cero = find(igdb.age_ratings, lambda r: r.organization.name == "CERO")
            return cero.rating_category.rating if cero else None

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
            return '|'.join(g.name for g in igdb.genres) if igdb.genres else None
            # Some string fields in RetroArch are treated as lists delimited by pipes, commas, or slashes.

        def get_pegi():
            pegi = find(igdb.age_ratings, lambda r: r.organization.name == "PEGI")
            return pegi.rating_category.rating if pegi else None

        def get_perspective():
            perspective = None
            if igdb.player_perspectives:
                perspective = '|'.join(p.name for p in igdb.player_perspectives)
            return perspective

        def get_publisher():
            publisher = None
            if igdb.involved_companies:
                publisher = '|'.join(c.company.name for c in igdb.involved_companies if c.publisher)
            return publisher

        def get_rumble():
            rumble_keyword = find(igdb.keywords, lambda k: k.id in RUMBLE_KEYWORD_IDS)
            if rumble_keyword:
                return True
            elif dat.rumble is not None:
                return dat.rumble
            return None

        def get_tags():
            keywords = (k.name.title() for k in igdb.keywords) if igdb.keywords else ()
            themes = (t.name for t in igdb.themes) if igdb.themes else ()
            tags = sorted(itertools.chain(keywords, themes))
            return '|'.join(tags) if tags else None

        def get_serial():
            if dat.serial:
                return dat.serial
            elif serial_rom := find(dat.rom, lambda r: r.serial is not None):
                return serial_rom.serial
            else:
                return None

        return DatGame(
            name=dat.name_key,
            rom=dat.rom,
            #achievements
            #analog
            #artstyle
            #bbfc_rating
            #category
            cero_rating=get_cero(),
            #code
            #console_exclusive
            #controls
            #coop
            #date
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
            #platform_exclusive
            publisher=get_publisher(),
            #region
            #releaseday
            #releasemonth
            #releaseyear
            rumble=get_rumble(),
            #score
            serial=get_serial(),
            #setting
            tags=get_tags(),
            #users
            #vehicular
            #version
            #visual
            #year

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
        if not hasheous_entry:
            if verbose:
                print(f"Warning: No Hasheous entry found for '{dat.name_key}' (CRC={crc}, MD5={md5}, SHA1={sha1}, serial={serial}, playlist='{playlist.playlist.title}')", file=sys.stderr)
            continue

        igdb_id = hasheous_entry.igdb_id
        if igdb_id is None:
            if verbose:
                print(f"Warning: No IGDB ID found in Hasheous entry for game '{dat.name_key}' (CRC {crc}) in playlist '{playlist.playlist.title}'", file=sys.stderr)
            continue

        igdb_entry = find(playlist.igdb, lambda g: g.id == igdb_id)
        if igdb_entry is None:
            if verbose:
                print(f"Warning: No IGDB entry found for IGDB ID {igdb_id} (Hasheous entry for game '{dat.name_key}' (CRC {crc}) in playlist '{playlist.playlist.title}')", file=sys.stderr)
            continue

        yield generate_game(dat, igdb_entry, hasheous_entry)

__all__ = ("PlaylistData", "generate_games")