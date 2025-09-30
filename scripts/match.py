from collections.abc import Collection, Iterable
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

        cero = find(igdb.age_ratings, lambda r: r.organization.name == "CERO")

        developer: str | None = None
        if igdb.involved_companies:
            developer = '|'.join(c.company.name for c in igdb.involved_companies if c.developer or c.porting)

        esrb = find(igdb.age_ratings, lambda r: r.organization.name == "ESRB")

        franchise = igdb.franchise.name if igdb.franchise else None
        # TODO: Handle multiple franchises

        genre = '|'.join(g.name for g in igdb.genres) if igdb.genres else None
        # Some string fields in RetroArch are treated as lists delimited by pipes, commas, or slashes.

        pegi = find(igdb.age_ratings, lambda r: r.organization.name == "PEGI")

        perspective: str | None = None
        if igdb.player_perspectives:
            perspective = '|'.join(p.name for p in igdb.player_perspectives)

        publisher: str | None = None
        if igdb.involved_companies:
            publisher = '|'.join(c.company.name for c in igdb.involved_companies if c.publisher)

        rumble_keyword = find(igdb.keywords, lambda k: k.id in RUMBLE_KEYWORD_IDS)
        if rumble_keyword:
            rumble = True
        elif dat.rumble is not None:
            rumble = dat.rumble
        else:
            rumble = None

        if dat.serial:
            serial = dat.serial
        elif serial_rom := find(dat.rom, lambda r: r.serial is not None):
            serial = serial_rom.serial
        else:
            serial = None


        return DatGame(
            name=dat.name_key,
            rom=dat.rom,
            #achievements
            #analog
            #artstyle
            #bbfc_rating
            #category
            cero_rating=cero.rating_category.rating if cero else None,
            #code
            #console_exclusive
            #controls
            #coop
            #date
            developer=developer,
            #download
            #edge_issue
            #edge_rating
            #elspa_rating
            #enhancement_hardware
            #enhancement_hw
            esrb_rating=esrb.rating_category.rating if esrb else None,
            #famitsu_rating
            franchise=franchise,
            #gameplay
            genre=genre,
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
            pegi_rating=pegi.rating_category.rating if pegi else None,
            perspective=perspective,
            #platform_exclusive
            publisher=publisher,
            #region
            #releaseday
            #releasemonth
            #releaseyear
            rumble=rumble,
            #score
            serial=serial
            #setting
            #tags
            #users
            #vehicular
            #version
            #visual
            #year

        )

    for dat in playlist.dats:
        crc = dat.crc_key
        hasheous_entry = find(playlist.hasheous, lambda d: d.has_crc(crc))
        if not hasheous_entry:
            if verbose:
                print(f"Warning: No Hasheous entry found for DAT game '{dat.name_key}' (CRC {crc}) in playlist '{playlist.playlist.title}'", file=sys.stderr)
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