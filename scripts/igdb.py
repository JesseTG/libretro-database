#!/usr/bin/env python3
import argparse
import asyncio
import os
import json
import sys
from typing import Optional

from authlib.integrations.httpx_client import AsyncOAuth2Client
from httpx import Response


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
        raise ValueError(f"Authentication failed: {str(e)}")


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
        'Accept': 'application/json'
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

        # Submit the query to IGDB
        response = await query_igdb(client, args.endpoint, args.query)

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
        print(f"Error: {str(e)}", file=sys.stderr)


def handle_process(args: argparse.Namespace) -> None:
    """Handle the process subcommand."""
    print(f"Process command called:")
    print(f"  Input directory: {args.indir}")
    print(f"  Output directory: {args.outdir}")


async def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Fetch and process game data from IGDB into RetroArch's DAT format.",
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
        help="Fetch data from the specified IGDB API endpoint and save it to the specified directory"
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
        help="The Apicalypse query to query data from"
    )
    query_parser.set_defaults(func=handle_query)

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