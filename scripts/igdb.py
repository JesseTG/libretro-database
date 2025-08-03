#!/usr/bin/env python3
import argparse
import asyncio
import itertools
import os
import json
import sys

from asyncio import TaskGroup
from contextlib import asynccontextmanager
from json import JSONDecodeError
from numbers import Number
from typing import Any, TypeAlias, TypedDict

import aiofiles
import aiofiles.os
import aiofiles.ospath
import asynciolimiter
import backoff
import httpx
from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2.rfc6749 import OAuth2Token
from httpx import Response, HTTPStatusError

from igdb_playlists import *

# TODO: Get game time to beat
# TODO: Get game characters

JsonPrimitive = str | int | float | bool | None
JsonArray: TypeAlias = "Sequence[JsonPrimitive | JsonObject | JsonArray]"
JsonObject: TypeAlias = "Mapping[str, JsonPrimitive | JsonArray | JsonObject]"

class MultiqueryResponse(TypedDict):
    name: str
    result: list[JsonObject]

class CountResponse(TypedDict, total=False):
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

    if args.endpoint == "multiquery":
        # Read multiquery definitions from file or stdin
        if args.query == '-':
            body = sys.stdin.read()
        else:
            with open(args.query, 'r') as f:
                body = f.read()
    else:
        body = args.query

    # Get token from Twitch API (IGDB uses Twitch authentication)
    async with authenticate_igdb(args) as (oauth, token):
        response = await query_igdb(oauth, args.endpoint, body)
        try:
            print(json.dumps(response.json(), indent=2))
        except JSONDecodeError as e:
            print(response.text, file=sys.stderr)
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

    # Limit to 8 in-flight requests
    request_limit = asyncio.BoundedSemaphore(MAX_ACTIVE_QUERIES)
    rate_limit = asynciolimiter.StrictLimiter(MAX_QUERY_RATE) # 4 requests per second
    outdir: str = args.outdir

    def giveup(e: Exception):
        print("Exception raised during query:", e, file=sys.stderr)
        if not isinstance(e, HTTPStatusError):
            # Give up if query_endpoint failed with something besides HTTPStatusError
            return True

        if e.response.status_code in RETRY_CODES:
            # Don't give up on server errors (5xx), we might just be unlucky
            # or rate-limited (429), so we should back off and retry.
            return False

        return e.response.is_error

    def on_backoff(details):
        print("Retrying after backoff:", details['target'].__name__, "with args:", details['args'], "and kwargs:", details['kwargs'], file=sys.stderr)

    def on_predicate(response: Response) -> bool:
        status = response.status_code
        if status in RETRY_CODES:
            # If the response is a retryable error, we want to retry
            print(f"Retrying due to status code {status} ({response.reason_phrase})", file=sys.stderr)
            return True

        return False

    @backoff.on_exception(backoff.expo, HTTPStatusError, max_tries=5, giveup=giveup, on_backoff=on_backoff)
    @backoff.on_predicate(backoff.expo, on_predicate)
    async def query_endpoint(client: AsyncOAuth2Client, endpoint: str, query: Query | Multiquery) -> Response:
        async with request_limit:
            try:
                await rate_limit.wait()
                return await query_igdb(client, endpoint, query)
            except HTTPStatusError as e:
                print(e.response.headers, file=sys.stderr)
                raise

    async def fetch_playlist(client: AsyncOAuth2Client, playlist: Playlist, group: TaskGroup) -> Sequence[Mapping[str, Any]]:
        print(f"{playlist.title}: Fetching game count in query...")
        response = await query_endpoint(client, "games/count", playlist.query)
        content_type = response.headers.get("Content-Type")
        if response.headers.get('content-type') != 'application/json':
            raise ValueError(f"Expected games/count response to be JSON for query to playlist {playlist.title}, got: {content_type} ({response.text})")
        count_json: JsonObject = response.json()

        if not isinstance(count_json, Mapping):
            raise ValueError(f"Expected count response for '{playlist.title}' query to be a JSON object; got: {type(count_json)} ({count_json})")

        if 'count' not in count_json:
            response.raise_for_status() # Raise an error if the response is not successful
            # But if the response is successful yet wrong, raise a ValueError
            raise ValueError(f"Count response for '{playlist.title}' lacks a 'count' attribute; got: {count_json} (Headers: {response.headers})")

        if not isinstance(count_json['count'], Number):
            raise ValueError(f"Expected response['count'] to be a number, got {type(count_json['count'])}")

        count = int(count_json['count'])
        multiqueries: list[Multiquery] = []
        for batch in itertools.batched(playlist.query_pages(count), MULTIQUERY_MAX):
            multiqueries.append(Multiquery({f"{playlist.title} ({q.offset}-{q.offset + q.limit - 1})": ('games', q) for q in batch}))

        playlist_tasks = tuple(group.create_task(query_endpoint(client, "multiquery", m)) for m in multiqueries)
        print(f"{playlist.title}: Scheduled to fetch {count} games...")

        responses: Sequence[Response]  = await asyncio.gather(*playlist_tasks)
        games: list[JsonObject] = []

        for r in responses:
            if r.is_error:
                print(f"Error fetching playlist {playlist.title}: {r.status_code} {r.reason_phrase}", file=sys.stderr)
                r.raise_for_status()

            content_type: str | None = r.headers.get('content-type')
            if content_type != 'application/json':
                raise ValueError(f"Expected multiquery response to be JSON for query to playlist {playlist.title}, got: {r.headers.get('content-type')} ({r.text})")

            try:
                response_json: Sequence[MultiqueryResponse] = r.json()
                if not isinstance(response_json, Sequence):
                    raise ValueError(f"Expected multiquery response for '{playlist.title}' to be a JSON array; got: {type(response_json)} ({response_json})")

                for g in response_json:
                    games.extend(g['result'])

            except JSONDecodeError as e:
                raise ValueError(f"Failed to decode JSON response for playlist {playlist.title}") from e
            except TypeError as e:
                raise ValueError(f"Unexpected response format for playlist {playlist.title}: {e}") from e

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

    async with authenticate_igdb(args) as (oauth, token):
        async with asyncio.TaskGroup() as group:
            tasks = tuple(group.create_task(fetch_playlist(oauth, p, group), name=p.title) for p in playlists)


def handle_process(args: argparse.Namespace) -> None:
    """Handle the process subcommand."""
    print(f"Process command called:")
    print(f"  Input directory: {args.indir}")
    print(f"  Output directory: {args.outdir}")


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Fetch and process game data from IGDB into the DAT format used by ClrMamePro and libretro.",
        prog="igdb"
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
        help="Fetch data from the specified IGDB API endpoint and print it to stdout"
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

    # Process subcommand
    process_parser = subparsers.add_parser(
        "process",
        help="Process saved data into RetroArch DAT format"
    )
    process_parser.add_argument(
        "indir",
        type=str,
        help="The input directory containing saved data"
    )
    process_parser.add_argument(
        "outdir",
        type=str,
        help="The output directory for the processed DAT files"
    )
    process_parser.set_defaults(func=handle_process)

    # Parse arguments and call appropriate handler

    args = parser.parse_args()
    asyncio.run(args.func(args))

if __name__ == "__main__":
    main()