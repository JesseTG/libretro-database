#!/usr/bin/env python3
import argparse
import asyncio
import os
import json
import sys
from typing import Any

from authlib.integrations.httpx_client import AsyncOAuth2Client
from httpx import Response

from igdb_playlists import *

# TODO: Get game time to beat
# TODO: Get game characters

def get_client_credentials(args: argparse.Namespace) -> tuple[str, str]:
    """Get client ID and secret from args or environment variables."""
    client_id = args.client_id or os.getenv('TWITCH_CLIENT_ID')
    client_secret = args.client_secret or os.getenv('TWITCH_CLIENT_SECRET')

    if not client_id or not client_secret:
        raise ValueError("Client ID and Client Secret are required for authentication")

    return client_id, client_secret


async def authenticate_igdb(args: argparse.Namespace) -> AsyncOAuth2Client:
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
    oauth = AsyncOAuth2Client(client_id=client_id, client_secret=client_secret)

    # Get token from Twitch API (IGDB uses Twitch authentication)
    token_url = f"https://id.twitch.tv/oauth2/token?client_id={client_id}&client_secret={client_secret}"

    return await oauth.fetch_token(
        token_url,
        grant_type='client_credentials',
    )


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

    # Authenticate with IGDB
    async with await authenticate_igdb(args) as client:
        # Submit the query to IGDB
        response = await query_igdb(client, args.endpoint, body)
        print(json.dumps(response.json(), indent=2))


async def handle_scrape(args: argparse.Namespace) -> None:
    """Handle the query subcommand."""

    playlist_args: Iterable[str] | None = args.playlist
    if not playlist_args:
        # If no playlists specified, use all known playlists
        playlist_args = (p.title for p in PLAYLISTS)

    # Get all playlists to scrape (filter out the Nones)
    playlists = tuple(filter(None, (get_playlist(p) for p in playlist_args)))
    if not playlists:
        raise ValueError("All listed playlists are unknown.")

    # Limit to 8 in-flight requests
    job_limit = asyncio.BoundedSemaphore(MAX_ACTIVE_QUERIES)

        # IGDB rate-limits us to 4 requests per second,
        # and allows up to 8 in-flight requests (in case some take longer than a second).
        semaphore = asyncio.BoundedSemaphore(8)  # Limit to 8 in-flight requests


        job_queue: asyncio.Queue[Coroutine] = asyncio.Queue()
        # IGDB rate-limits us to 4 requests per second


        # TODO: Build the job queue

        # TODO: for each named playlist:
        #   - Query IGDB for the number of games
        #   - Build a multiquery to fetch all games in the playlist
        #   - Sort the games by name
        #   - Save the games to the specified output directory as JSON files
    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)


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
        "--playlist",
        type=str,
        help="The name of the playlists to scrape. Can be given multiple times. If not provided, all playlists will be scraped.",
        action="append"
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