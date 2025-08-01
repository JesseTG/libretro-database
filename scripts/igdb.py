#!/usr/bin/env python3
import argparse
import asyncio
import os
import json
import sys
from collections.abc import Coroutine
from time import time_ns
from typing import Optional, Any

from authlib.integrations.httpx_client import AsyncOAuth2Client
from httpx import Response

import igdb_playlists

# fields name, age_ratings.organization.name, age_ratings.rating_category.rating, age_ratings.rating_content_descriptions.description, aggregated_rating, aggregated_rating_count, alternative_names.name, alternative_names.comment, first_release_date, forks.name, franchise.name, franchises.name, game_localizations.name, game_localizations.region.name, game_localizations.region.identifier, game_localizations.region.category, game_modes.name, game_status.status, game_type.type, genres.name, involved_companies.company.name, involved_companies.company.country, involved_companies.company.description, involved_companies.company.status.name, involved_companies.developer, involved_companies.porting, involved_companies.publisher, involved_companies.supporting, keywords.name, language_supports.language.locale, language_supports.language.name, language_supports.language_support_type.name, multiplayer_modes.campaigncoop, multiplayer_modes.dropin, multiplayer_modes.lancoop, multiplayer_modes.lancoop, multiplayer_modes.offlinecoop, multiplayer_modes.offlinecoopmax, multiplayer_modes.offlinemax, multiplayer_modes.onlinecoop, multiplayer_modes.onlinecoopmax, multiplayer_modes.onlinemax, multiplayer_modes.splitscreen, multiplayer_modes.splitscreenonline, platforms.name, platforms.abbreviation, platforms.alternative_name, platforms.generation, platforms.platform_family.name, platforms.platform_type.name, platforms.slug, platforms.summary, platforms.versions.name, platforms.versions.connectivity, platforms.versions.cpu, player_perspectives.name, ports, release_dates.date, release_dates.human, release_dates.m, release_dates.y, release_dates.date_format.format, release_dates.release_region.region, release_dates.status.description, release_dates.status.name, remakes, remasters, similar_games, slug, standalone_expansions, dlcs, expanded_games, expansions, external_games, storyline, summary, tags, themes.name, version_title, websites.url, websites.type.type, websites.trusted;
# TODO: Get game time to beat
# TODO: Get game characters

def get_client_credentials(args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    """Get client ID and secret from args or environment variables."""
    client_id = args.client_id or os.getenv('TWITCH_CLIENT_ID')
    client_secret = args.client_secret or os.getenv('TWITCH_CLIENT_SECRET')
    return client_id, client_secret


async def authenticate_igdb(client_id: str, client_secret: str) -> AsyncOAuth2Client:
    """
    Authenticate with IGDB API using OAuth 2.0 Client Credentials flow.

    Args:
        client_id: The IGDB API client ID
        client_secret: The IGDB API client secret

    Returns:
        An authenticated AsyncOAuth2Client instance

    Raises:
        ValueError: If authentication fails
    """
    if not client_id or not client_secret:
        raise ValueError("Client ID and Client Secret are required for authentication")

    # Set up OAuth 2.0 client with client credentials flow
    oauth = AsyncOAuth2Client(client_id=client_id, client_secret=client_secret)

    # Get token from Twitch API (IGDB uses Twitch authentication)
    token_url = f"https://id.twitch.tv/oauth2/token?client_id={client_id}&client_secret={client_secret}"

    try:
        await oauth.fetch_token(
            token_url,
            grant_type='client_credentials',
        )
        if not oauth.token:
            raise ValueError("Failed to obtain access token")

        return oauth

    except Exception as e:
        raise ValueError(f"Authentication failed: {str(e)}") from e


async def query_igdb(client: AsyncOAuth2Client, endpoint: str, query: str) -> Response:
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

    return await client.post(url, headers=headers, content=query)


async def handle_query(args: argparse.Namespace) -> None:
    """Handle the query subcommand."""
    client_id, client_secret = get_client_credentials(args)

    if not client_id or not client_secret:
        print("Error: Client ID and Client Secret are required. Provide them as arguments or environment variables.", file=sys.stderr)
        return

    try:
        # Authenticate with IGDB
        client = await authenticate_igdb(client_id, client_secret)

        if args.endpoint == "multiquery":
            # Read multiquery definitions from file or stdin
            if args.query == '-':
                body = sys.stdin.read()
            else:
                with open(args.query, 'r') as f:
                    body = f.read()
        else:
            # Just pass the argument directly to the API and say it's a query
            body = args.query

        # Submit the query to IGDB
        response = await query_igdb(client, args.endpoint, body)

        # Print the response
        if response.status_code == 200:
            try:
                # Pretty print JSON response
                json_response = response.json()
                print(json.dumps(json_response, indent=2))
            except ValueError:
                # If response is not JSON
                print("Response (not JSON):", file=sys.stderr)
                print(response.text, file=sys.stderr)
        else:
            print("Error response:", file=sys.stderr)
            print(response.text)

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)


async def handle_scrape(args: argparse.Namespace) -> None:
    """Handle the query subcommand."""
    client_id, client_secret = get_client_credentials(args)

    if not client_id or not client_secret:
        print("Error: Client ID and Client Secret are required. Provide them as arguments or environment variables.", file=sys.stderr)
        return

    try:
        # Authenticate with IGDB
        client = await authenticate_igdb(client_id, client_secret)
        last_request = time_ns()

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