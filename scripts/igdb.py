#!/usr/bin/env python3
import argparse
import asyncio
import itertools
import os
import json
import sys

from asyncio import TaskGroup
from collections.abc import Sequence, Iterable, Mapping, Iterator
from contextlib import asynccontextmanager
from json import JSONDecodeError
from typing import Any, TypeAlias, TypedDict, cast

import aiofiles
import aiofiles.os
import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2.rfc6749 import OAuth2Token
from httpx import Response

from igdb_playlists import *

# TODO: Get game time to beat
# TODO: Get game characters

JsonPrimitive = str | int | float | bool | None
JsonArray: TypeAlias = "Sequence[JsonPrimitive | JsonObject | JsonArray]"
JsonObject: TypeAlias = "Mapping[str, JsonPrimitive | JsonArray | JsonObject]"

class GameResponse(TypedDict):
    name: str

class MultiqueryResponse(TypedDict):
    name: str
    result: Sequence[GameResponse]

class CountResponse(TypedDict):
    count: int

def get_client_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Get client ID and secret from args or environment variables."""
    client_id = args.client_id or os.getenv('TWITCH_CLIENT_ID')
    client_secret = args.client_secret or os.getenv('TWITCH_CLIENT_SECRET')

    if not client_id or not client_secret:
        raise ValueError("Client ID and Client Secret are required for authentication")

    return client_id, client_secret

@asynccontextmanager
async def authenticate_igdb(args: argparse.Namespace):
    """
    Authenticate with IGDB API using OAuth 2.0 Client Credentials flow.

    Args:
        args: The parsed command line arguments containing client ID and secret.

    Returns:
        An authenticated AsyncOAuth2Client instance

    Raises:
        An exception if authentication fails or if the client ID/secret are not provided.
    """
    client_id, client_secret = get_client_credentials(args)

    # Set up OAuth 2.0 client with client credentials flow
    # TODO: Make the timeout configurable
    async with AsyncOAuth2Client(client_id=client_id, client_secret=client_secret, timeout=httpx.Timeout(None)) as oauth:
        # Get token from Twitch API (IGDB uses Twitch authentication)
        token_url = f"https://id.twitch.tv/oauth2/token?client_id={client_id}&client_secret={client_secret}"

        token: OAuth2Token = await oauth.fetch_token(
            token_url,
            grant_type='client_credentials',
        )
        yield oauth, token


async def query_igdb(client: AsyncOAuth2Client, endpoint: str, query: str | Query | Multiquery) -> Response:
    """
    Query the IGDB API with the given endpoint and query.

    Args:
        client: The authenticated AsyncOAuth2Client instance
        endpoint: The IGDB API endpoint to query
        query: The Apicalypse query string to send to the endpoint

    Returns:
        The HTTP response from the API

    Raises:
        requests.exceptions.RequestException: If the request fails
    """
    url = f"https://api.igdb.com/v4/{endpoint}"
    access_token = client.token["access_token"]

    headers = {
        'Client-ID': client.client_id,
        'Authorization': f'Bearer {access_token}',
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip, deflate'
    }

    return await client.post(url, headers=headers, content=str(query))


async def handle_query(args: argparse.Namespace) -> None:
    """Handle the query subcommand."""

    all_records = bool(args.all)
    verbose = bool(args.verbose)

    if args.endpoint == "multiquery":
        # Read multiquery definitions from file or stdin
        if args.query == '-':
            body = sys.stdin.read()
        else:
            with open(args.query, 'r') as f:
                body = f.read()
    else:
        body = args.query

    client_id, client_secret = get_client_credentials(args)
    async with QueryClient(client_id, client_secret) as client:
        try:
            if not all_records:
                # If the user didn't pass the --all flag...
                response = await client.query(args.endpoint, body)
                json.dump(response, sys.stdout, indent=2)
            else:
                count_response = cast(CountResponse, await client.query(f"{args.endpoint}/count", body))
                count = count_response["count"]
                if verbose:
                    print(f"Query will return {count} total records", file=sys.stderr)

                query = Query(body)
                async with TaskGroup() as group:
                    tasks: list[asyncio.Task[JsonArray]] = []
                    for q in itertools.batched(query.query_pages(count), MULTIQUERY_MAX):
                        if verbose:
                            print(f"Fetching records {q[0].offset} to {q[-1].offset + q[-1].limit - 1}", file=sys.stderr)

                        multiquery = Multiquery({f"{args.endpoint} ({p.offset}-{p.offset + p.limit - 1})": (args.endpoint, p) for p in q})
                        task = group.create_task(client.query("multiquery", multiquery))
                        tasks.append(task)

                    responses: Sequence[Sequence[MultiqueryResponse]] = await asyncio.gather(*tasks) # type: ignore[type-var]


                results = tuple(r['result'] for r in itertools.chain.from_iterable(responses))
                records = tuple(itertools.chain.from_iterable(results))
                json.dump(records, sys.stdout, indent=2)
        except JSONDecodeError as e:
            print(e.doc, file=sys.stderr)
            print(e, file=sys.stderr)
            raise e

RETRY_CODES = (
    httpx.codes.REQUEST_TIMEOUT,
    httpx.codes.TOO_MANY_REQUESTS,
    httpx.codes.INTERNAL_SERVER_ERROR,
    httpx.codes.BAD_GATEWAY,
    httpx.codes.SERVICE_UNAVAILABLE,
    httpx.codes.GATEWAY_TIMEOUT,
)

async def handle_scrape(args: argparse.Namespace) -> None:
    """Handle the query subcommand."""

    playlist_args: Iterable[str] | None = args.playlists
    if not playlist_args:
        # If no playlists specified, use all known playlists
        playlist_args = (p.title for p in PLAYLISTS)

    # Get all playlists to scrape (filter out the Nones)
    playlists = tuple(filter(None, (get_playlist(p) for p in playlist_args)))
    if not playlists:
        raise ValueError("All listed playlists are unknown.")

    outdir: str = args.outdir

    async def fetch_playlist(client: QueryClient, playlist: Playlist, group: TaskGroup) -> Sequence[Mapping[str, Any]]:
        print(f"{playlist.title}: Fetching game count in query...")
        count = await client.count("games", playlist.query)

        multiqueries: list[Multiquery] = []
        for batch in itertools.batched(playlist.query_pages(count), MULTIQUERY_MAX):
            multiqueries.append(Multiquery({f"{playlist.title} ({q.offset}-{q.offset + q.limit - 1})": ('games', q) for q in batch}))

        playlist_tasks = tuple(group.create_task(client.query("multiquery", m)) for m in multiqueries)
        print(f"{playlist.title}: Scheduled to fetch {count} games...")

        responses: Sequence[JsonArray]  = await asyncio.gather(*playlist_tasks)
        games: list[GameResponse] = []

        for r in responses:
            if not isinstance(r, Sequence):
                raise ValueError(f"Expected multiquery response for '{playlist.title}' to be a JSON array; got: {type(r)} ({r})")

            for g in cast(Sequence[MultiqueryResponse], r):
                games.extend(g['result'])
                # We're not processing the returned games except to sort them,
                # so we don't need to convert them to IgdbGame objects here.

        print(f"{playlist.title}: Fetched {len(games)} games.")
        games.sort(key=lambda g: g['name'])
        # Now that we have all the games, sort them by name

        # Create the output directory if it doesn't exist
        await aiofiles.os.makedirs(outdir, exist_ok=True)
        outpath = os.path.join(outdir, f"{playlist.title}.json")
        async with aiofiles.open(outpath, 'w', encoding='utf-8') as outfile:
            await outfile.write(json.dumps(games, indent=2, ensure_ascii=False))
            print(f"{playlist.title}: Saved {len(games)} games to {outpath}")

        return games

    client_id, client_secret = get_client_credentials(args)
    async with QueryClient(client_id, client_secret) as client:
        async with asyncio.TaskGroup() as group:
            tasks = tuple(group.create_task(fetch_playlist(client, p, group), name=p.title) for p in playlists)

def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Fetch and process game data from IGDB into the DAT format used by ClrMamePro and libretro.",
        prog="igdb"
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show more logging output"
    )
    # Create subparsers for commands
    subparsers = parser.add_subparsers(
        dest="command",
        help="Available commands",
        required=True
    )

    # Query subcommand
    query_parser = subparsers.add_parser(
        "query",
        help="Make a request to an IGDB API endpoint and print the response to stdout."
    )
    query_parser.add_argument(
        "--client-id",
        type=str,
        help="The IGDB API client ID. Overrides the TWITCH_CLIENT_ID environment variable if provided."
    )
    query_parser.add_argument(
        "--client-secret",
        type=str,
        help="The IGDB API client secret. Overrides the TWITCH_CLIENT_SECRET environment variable if provided."
    )
    query_parser.add_argument(
        "--all",
        action="store_true",
        help="Use this query, but fetch all results by making multiple requests."
    )
    query_parser.add_argument(
        "endpoint",
        type=str,
        help="The IGDB API endpoint to query data from"
    )
    query_parser.add_argument(
        "query",
        type=str,
        help="The Apicalypse query to query data from. If 'endpoint' is 'multiquery', this should be a path to a query file or '-' to read from stdin."
    )
    query_parser.set_defaults(func=handle_query)

    # Scrape subcommand
    scrape_parser = subparsers.add_parser(
        "scrape",
        help="Scrape data from IGDB and save it to the specified directory"
    )
    scrape_parser.add_argument(
        "--client-id",
        type=str,
        help="The IGDB API client ID. Overrides the TWITCH_CLIENT_ID environment variable if provided."
    )
    scrape_parser.add_argument(
        "--client-secret",
        type=str,
        help="The IGDB API client secret. Overrides the TWITCH_CLIENT_SECRET environment variable if provided."
    )
    scrape_parser.add_argument(
        "--playlists",
        type=str,
        help="The title or system IDs of the playlists to scrape. If not provided, all known playlists will be scraped.",
        action="extend",
        nargs="*",
        default=PLAYLISTS_BY_TITLE.keys()  # Default to all known playlists
    )
    scrape_parser.add_argument(
        "outdir",
        type=str,
        help="The output directory for the scraped JSON files"
    )
    scrape_parser.set_defaults(func=handle_scrape)

    # Parse arguments and call appropriate handler

    args = parser.parse_args()
    asyncio.run(args.func(args))

if __name__ == "__main__":
    main()