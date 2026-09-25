"""Run the legacy domain redirect daemon.

    python -m legacy_redirect --old old.example.com --new wiki.example.net
    python -m legacy_redirect --old old.example.com --new wiki.example.net --port 8090
    python -m legacy_redirect --old a.example.com --old b.example.org \
        --new wiki.example.net

Both domains are required, from these options or from LEGACY_OLD_DOMAINS
and LEGACY_NEW_DOMAIN in the environment.
"""

import argparse
import os
from wsgiref.simple_server import make_server


def main():
    parser = argparse.ArgumentParser(
        description="Serve a 'we moved' page on wildcard subdomains of old domains.",
    )
    parser.add_argument(
        "--host", default=os.environ.get("LEGACY_BIND_HOST", "0.0.0.0"),
        help="Address to bind to (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("LEGACY_BIND_PORT", "8090")),
        help="Port to listen on (default: 8090)",
    )
    parser.add_argument(
        "--old", action="append", default=None,
        help="Old domain(s) to match subdomains against (repeatable)",
    )
    parser.add_argument(
        "--new", default=None,
        help="New domain to redirect to (required)",
    )
    args = parser.parse_args()

    if args.old:
        os.environ["LEGACY_OLD_DOMAINS"] = ",".join(args.old)
    if args.new:
        os.environ["LEGACY_NEW_DOMAIN"] = args.new

    # Import after env is set so the module picks up the values
    try:
        from .app import application as wsgi_app, OLD_DOMAINS, NEW_DOMAIN
    except RuntimeError as exc:
        parser.error(str(exc))

    server = make_server(args.host, args.port, wsgi_app)
    domains_str = ", ".join("*.{}".format(d) for d in OLD_DOMAINS)
    print(
        "Legacy redirect daemon listening on {}:{}\n"
        "  Old domains: {}\n"
        "  New domain:  *.{}\n"
        "  Press Ctrl+C to stop.".format(args.host, args.port, domains_str, NEW_DOMAIN)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.server_close()


if __name__ == "__main__":
    main()
