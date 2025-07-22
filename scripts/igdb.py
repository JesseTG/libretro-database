#!/usr/bin/env python3
import argparse
import os
from typing import Optional


def get_client_credentials(args: argparse.Namespace) -> tuple[Optional[str], Optional[str]]:
    """Get client ID and secret from args or environment variables."""
    client_id = args.client_id or os.getenv('TWITCH_CLIENT_ID')
    client_secret = args.client_secret or os.getenv('TWITCH_CLIENT_SECRET')
    return client_id, client_secret


def handle_fetch(args: argparse.Namespace) -> None:
    """Handle the fetch subcommand."""
    client_id, client_secret = get_client_credentials(args)
    print(f"Fetch command called:")
    print(f"  Endpoint: {args.endpoint}")
    print(f"  Query: {args.query}")
    print(f"  Client ID: {client_id}")
    print(f"  Client Secret: {'***' if client_secret else None}")


def handle_process(args: argparse.Namespace) -> None:
    """Handle the process subcommand."""
    print(f"Process command called:")
    print(f"  Input directory: {args.indir}")
    print(f"  Output directory: {args.outdir}")


def main() -> None:
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

    # Fetch subcommand
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="Fetch data from the specified IGDB API endpoint and save it to the specified directory"
    )
    fetch_parser.add_argument(
        "--client-id",
        type=str,
        help="The IGDB API client ID. Overrides the TWITCH_CLIENT_ID environment variable if provided."
    )
    fetch_parser.add_argument(
        "--client-secret",
        type=str,
        help="The IGDB API client secret. Overrides the TWITCH_CLIENT_SECRET environment variable if provided."
    )
    fetch_parser.add_argument(
        "endpoint",
        type=str,
        help="The IGDB API endpoint to fetch data from"
    )
    fetch_parser.add_argument(
        "query",
        type=str,
        help="The Apicalypse query to fetch data from"
    )
    fetch_parser.set_defaults(func=handle_fetch)

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
    args.func(args)


if __name__ == "__main__":
    main()