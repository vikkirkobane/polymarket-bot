"""
main.py — Polymarket Claude Trading Bot entry point.

Usage:
    python main.py --mode paper --scan --analyze --report
    python main.py --mode paper --scan --analyze --trade
    python main.py --mode live --scan --analyze --trade     # Real money!

Run `python main.py --help` for full options.
"""

import argparse
import sys
import os
from pathlib import Path

# ── Safety Banner ──────────────────────────────────────────────────────────────

PAPER_BANNER = """
╔══════════════════════════════════════════════════════════╗
║           PAPER TRADING MODE — NO REAL MONEY            ║
║   All trades are simulated and logged locally only.     ║
╚══════════════════════════════════════════════════════════╝
"""

LIVE_BANNER = """
╔══════════════════════════════════════════════════════════╗
║          ⚠️  LIVE TRADING MODE — REAL MONEY ⚠️           ║
║   Orders will be submitted to Polymarket's CLOB.        ║
║   You are responsible for all trades placed.            ║
╚══════════════════════════════════════════════════════════╝
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Polymarket Claude Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Safe paper trading run (recommended to start)
  python main.py --mode paper --scan --analyze --report

  # Full paper trading cycle with simulated execution
  python main.py --mode paper --scan --analyze --trade --report

  # Only analyze top 20 markets, minimum 12% edge
  python main.py --scan --analyze --limit 20 --min-edge 12

  # Live mode (real trades — only when ready!)
  python main.py --mode live --scan --analyze --trade
        """,
    )

    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode: 'paper' (safe, default) or 'live' (real money)",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Fetch markets from Polymarket API",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Run Claude analysis on fetched markets",
    )
    parser.add_argument(
        "--trade",
        action="store_true",
        help="Execute trades based on analysis",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print daily summary report",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Number of markets to scan (default: 50)",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=None,
        help="Minimum edge %% to recommend a bet (default: from .env)",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=1000,
        help="Minimum 24h volume USD to include market (default: 1000)",
    )
    parser.add_argument(
        "--pnl",
        action="store_true",
        help="Show P&L summary from trade history",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    # ── Environment setup ──────────────────────────────────────────────────────
    # Override paper_mode from CLI flag
    if args.mode == "live":
        os.environ["PAPER_MODE"] = "false"
    else:
        os.environ["PAPER_MODE"] = "true"

    if args.min_edge is not None:
        os.environ["MIN_EDGE_PERCENT"] = str(args.min_edge)

    # ── Import after env setup ─────────────────────────────────────────────────
    try:
        from config import get_config
        cfg = get_config()
    except ValueError as e:
        print(f"\n[CONFIG ERROR] {e}\n")
        print("Run: cp .env.example .env  then fill in your keys.")
        sys.exit(1)

    from polymarket import PolymarketClient
    from analyst import ClaudeAnalyst
    from trader import Trader
    import logger

    # ── Banner ─────────────────────────────────────────────────────────────────
    logger.print_header()

    if cfg.paper_mode:
        print(PAPER_BANNER)
    else:
        print(LIVE_BANNER)
        confirm = input("Type 'CONFIRM' to proceed with live trading: ")
        if confirm != "CONFIRM":
            print("Aborted.")
            sys.exit(0)

    # ── P&L Summary ───────────────────────────────────────────────────────────
    if args.pnl:
        trader = Trader()
        pnl = trader.get_pnl_summary()
        logger.console.print("\n[bold]P&L SUMMARY[/bold]")
        for k, v in pnl.items():
            logger.console.print(f"  {k}: {v}")
        return

    # ── Scan ──────────────────────────────────────────────────────────────────
    markets = []
    if args.scan:
        logger.console.print(f"[cyan]SCAN[/cyan]  Fetching top {args.limit} markets...")
        client = PolymarketClient()
        try:
            all_markets = client.fetch_active_markets(
                limit=args.limit,
                min_volume=args.min_volume,
            )
            markets = all_markets
            logger.print_market_scan(args.limit, len(markets))
        except Exception as e:
            logger.console.print(f"[red]ERROR fetching markets:[/red] {e}")
            sys.exit(1)

        if not markets:
            logger.console.print(
                "[yellow]No markets passed filters. Try --min-volume 500 or --limit 100[/yellow]"
            )
            sys.exit(0)

    # ── Analyze ───────────────────────────────────────────────────────────────
    report = None
    if args.analyze:
        if not markets:
            logger.console.print("[yellow]Run --scan first to fetch markets.[/yellow]")
            sys.exit(1)

        logger.console.print(
            f"\n[cyan]ANALYZE[/cyan]  Sending {len(markets)} markets to Claude...\n"
        )
        analyst = ClaudeAnalyst()
        try:
            report = analyst.analyze_batch(markets)
        except Exception as e:
            logger.console.print(f"[red]ERROR during analysis:[/red] {e}")
            sys.exit(1)

        if args.report or not args.trade:
            logger.print_report(report)
            logger.save_report_json(report)

    # ── Trade ─────────────────────────────────────────────────────────────────
    if args.trade:
        if report is None:
            logger.console.print("[yellow]Run --analyze first.[/yellow]")
            sys.exit(1)

        if report.no_edge_today or not report.bets:
            logger.console.print("\n[yellow]NO EDGE TODAY — no trades placed.[/yellow]")
            return

        trader = Trader()
        records = trader.execute_report(report.bets)
        logger.print_trade_records(records)

        if args.report:
            logger.print_report(report)

    # ── No flags = show help ──────────────────────────────────────────────────
    if not any([args.scan, args.analyze, args.trade, args.pnl]):
        logger.console.print(
            "[dim]No action flags set. Use --scan --analyze --report to run.[/dim]\n"
            "[dim]Run python main.py --help for full options.[/dim]"
        )


if __name__ == "__main__":
    main()
