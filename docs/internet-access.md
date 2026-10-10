# API keys and Internet access

Hub can sit behind an HTTPS reverse proxy and require a key for every data/API
request. It still binds only a literal loopback/private/VPN address. The login
page and its static JS/CSS are public; history, knowledge, tools, health, models,
mobile sync and conversations require authentication.

This is a **single-owner** service. Each owner key grants full API
access, including configured agents and node actions; keys are not separate user
accounts or per-node grants. Issue them only to your own trusted clients.
Webhook keys can only publish incidents; MCP keys/grants can only access the
[dot MCP endpoint](dot-integration.md) and its bounded tools for active requests.

## Create a client key

The existing private `token` file remains the owner's master key. Local launcher,
MCP, node and Coucou connections continue to work. For an external client, create
a separate revocable key instead of distributing the master key:

```sh
python3 tools/hub_keys.py create external-client \
  --output "$HOME/.local/share/assistant-stack/external-client.key"
python3 tools/hub_keys.py list
python3 tools/hub_keys.py revoke KEY_UUID_FROM_LIST
```

For another runtime, put `--config /absolute/private/runtime/config.json` before
the command. Creation writes a **new** mode-0600 file, refuses overwrites/symlinks
and prints only the key ID, name and path. The random secret is not printed or
stored in conversations. Only its SHA-256 digest is stored in private SQLite.
There is a limit of 128 active client keys. Creation/revocation is local CLI
administration; no owner HTTP endpoint issues API keys. The optional dot OAuth
flow issues separate access/refresh tokens only after owner consent.

Send the secret as `Authorization: Bearer <key>`, including for the standard
`/v1/models` and `/v1/chat/completions` facade. Keys in query strings are ignored.
No CORS access from arbitrary websites is enabled. Keep keys out of URLs, logs
and source control. The `.key` files are ignored by Git.

The CLI below supplies the header through curl's stdin rather than process
arguments (replace the example hostname):

```sh
{ printf 'Authorization: Bearer '; cat "$HOME/.local/share/assistant-stack/external-client.key"; } \
  | curl --fail-with-body --header @- https://hub.example.com/health
```

Absent/incorrect keys return **401** with `WWW-Authenticate: Bearer`. Repeated
failed authentication returns **429**, `Retry-After: 60` (20 failures per peer or
200 total in a one-minute window). Correct keys remain usable during that window.
The proxy should also bound connections/request rates. A revoked key and browser
sessions/tickets created with it are rejected on their next request without a
restart. Tasks already submitted are not retroactively cancelled; use `/api/cancel`
if needed. To rotate the legacy master token, replace its private file and restart
Hub, then update clients still using that token.

## Configure an HTTPS origin

Add these fields to your **private** runtime `config.json`, keeping providers,
state paths, token and other settings intact:

```json
{
  "host": "127.0.0.1",
  "port": 8778,
  "public_url": "https://hub.example.com",
  "trusted_proxies": ["127.0.0.1"]
}
```

Use your actual domain and the IP that Hub sees as its proxy's TCP peer. Both
settings are required together; `public_url` must be a canonical lowercase HTTPS
origin without a path/query/credentials. Explicit ports and bracketed IPv6
addresses are supported. Trusted proxies accept loopback/private/VPN IPs or
canonical subnets; prefer individual addresses. Arbitrary public IPs and wildcard
ranges are rejected. Restart Hub after changing these transport settings.

Public requests must have exactly the configured Host, `X-Forwarded-Proto: https`,
and a trusted TCP peer. A request's Origin, if present, must match that HTTPS
origin. Forwarded headers on the direct local origin are rejected. Hub never uses
an arbitrary forwarded hostname to select an origin. An optional
`X-Forwarded-For` must be one IP address, **overwritten** by the trusted proxy;
it is used only to count failed authentication, not to authorize access.

The browser accepts the same API key at login, then uses a 24-hour HttpOnly,
SameSite=Strict cookie, with **Secure** on the public HTTPS origin. Browser
sessions and one-use 60-second login tickets are bound to their issuing origin;
cookie-authenticated POSTs also require that exact Origin. Tokens are not put in
localStorage. Cookies/tickets disappear on Hub restart.

## HAProxy example

This example terminates TLS on the same host as Hub. Substitute your domain and
certificate path. Port 8778 remains on loopback; publish only the HTTPS frontend.
For a proxy on another machine, use an explicit private/VPN Hub listener and
the proxy's actual source IP. Keep the proxy-to-Hub hop local or inside an
encrypted VPN. A plain LAN hop does not encrypt bearer keys.

```haproxy
global
    maxconn 256

defaults
    mode http
    timeout connect 5s
    timeout client 300s
    timeout server 300s
    timeout http-request 10s

frontend assistant_https
    bind :443 ssl crt /etc/haproxy/certs/hub.example.com.pem
    # Do not log URLs (one-use tickets) or credential headers.
    no log
    acl hub_host hdr(host) -i hub.example.com hub.example.com:443
    http-request deny unless hub_host
    http-request deny if { path_beg /coucou/ }
    http-request del-header Forwarded
    http-request del-header X-Forwarded-Host
    http-request set-header Host hub.example.com
    http-request set-header X-Forwarded-Proto https
    http-request set-header X-Forwarded-For %[src]
    http-response set-header Strict-Transport-Security "max-age=31536000"
    default_backend assistant_local

backend assistant_local
    server hub 127.0.0.1:8778 maxconn 64 check
```

The `check` here is a TCP check; Hub's `/health` intentionally needs a key.
Header rewrites use HAProxy's documented
[set-header / del-header rules](https://www.haproxy.com/documentation/haproxy-configuration-tutorials/proxying-essentials/http-rewrites/).
Validate your complete proxy configuration before reload with
`haproxy -c -f /etc/haproxy/haproxy.cfg`. DNS and a valid certificate are required
for a real external client; the example does not configure them automatically.

Coucou's original fixed `Bearer ollama` plus private capability URL is accepted
**only on direct loopback**, never through the public proxy or a LAN/VPN listener.
External clients use a real Hub bearer key and `/v1/...`. Ollama, agent runtimes
and their management ports remain internal; the HTTPS frontend exposes Hub alone.

Verify the deployment: an unauthenticated `GET /health` returns 401, a valid key
returns 200, and a revoked client key returns 401 while another key still works.
Verify the external browser login and a real client separately; local HTTP tests
exercise proxy headers but do not establish your DNS/certificate/firewall setup.
