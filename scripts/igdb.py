#!/usr/bin/env python3
import argparse
import asyncio
import os
import json
import sys
from asyncio import get_event_loop
from contextlib import asynccontextmanager
from json import JSONDecodeError
from typing import Any, TypeAlias, TypedDict

from authlib.integrations.httpx_client import AsyncOAuth2Client
from authlib.oauth2.rfc6749 import OAuth2Token
from httpx import Response

from igdb_playlists import *

# TODO: Get game time to beat
# TODO: Get game characters

JsonPrimitive = str | int | float | bool | None
JsonArray: TypeAlias = "Sequence[JsonPrimitive | JsonObject]"
JsonObject: TypeAlias = "Mapping[str, JsonPrimitive | JsonArray | JsonObject]"

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
    async with AsyncOAuth2Client(client_id=client_id, client_secret=client_secret) as oauth:
        # Get token from Twitch API (IGDB uses Twitch authentication)
        token_url = f"https://id.twitch.tv/oauth2/token?client_id={client_id}&client_secret={client_secret}"

        token: OAuth2Token = await oauth.fetch_token(
            token_url,
            grant_type='client_credentials',
        )
        yield oauth, token


async def query_igdb(client: AsyncOAuth2Client, endpoint: str, query: str | Query) -> Response:
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
    last_request_time = 0.0

    # IGDB rate-limits us to 4 requests per second,
    # and allows up to 8 in-flight requests (in case some take longer than a second).
    async def query_endpoint(client: AsyncOAuth2Client, endpoint: str, query: Query) -> Response:
        # IGDB limits us to 8 in-flight requests
        # TODO: Honor the rate limit of 4 requests per second
        now = get_event_loop().time()
        async with request_limit:
            return await query_igdb(client, endpoint, query)

    async def fetch_playlist(client: AsyncOAuth2Client, playlist: Playlist) -> Sequence[Mapping[str, Any]]:
        response = await query_endpoint(client, "games/count", playlist.query)
        count_json: JsonObject = response.json()

        if not isinstance(count_json, Mapping):
            raise ValueError(f"Expected count response to be a JSON object; got: {type(count_json)}")

        if 'count' not in count_json:
            response.raise_for_status() # Raise an error if the response is not successful
            # But if the response is successful yet wrong, raise a ValueError
            raise ValueError(f"Count response lacks a 'count' attribute; got: {count_json}")

        print(f"'{playlist.title}' has {count_json['count']} games.")

        # TODO: Build a series of multiqueries to fetch all games in the playlist (navigate all pages)
        # TODO: Fetch these multiqueries in parallel, using the semaphore to maintain the job limit
        # TODO: Keep these responses in memory until all queries are done
        # TODO: Sort the list by name
        # TODO: Save the playlist JSON to the specified output directory, in a file named after the playlist title
        pass

    async with authenticate_igdb(args) as (oauth, token):
        #response = await query_igdb(oauth, args.endpoint, body)

        async with asyncio.TaskGroup() as group:
            tasks = tuple(group.create_task(fetch_playlist(oauth, p)) for p in playlists)


def handle_process(args: argparse.Namespace) -> None:
    """Handle the process subcommand."""
    print(f"Process command called:")
    print(f"  Input directory: {args.indir}")
    print(f"  Output directory: {args.outdir}")


async def main():
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
    await args.func(args)


if __name__ == "__main__":
    asyncio.run(main())