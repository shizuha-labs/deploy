# Run your own Shizuha platform

Install Home, ID, Admin, Hive, Pulse, Connect, Wiki, Atlas, Notes, Drive and Mail on your
own machine. This repository contains the installer and a source lock for the
reviewed public service repositories. The build fetches those exact commits;
you do not need a Shizuha account or access to our infrastructure.

## Requirements

Use Linux amd64 with Git, Python 3.12 or newer, Docker Engine, and Docker Compose
v2. Docker must support privileged containers: Hive runs agents in a dedicated
K3s container with its own persistent cluster state. The installer does not mount
your Docker socket or host kubeconfig. ARM installation is not yet verified.

You need internet access to GitHub and public package/container registries during
the source build. Running an AI agent also requires your own OpenAI-compatible
inference endpoint and provider key. Mail delivery outside the installation
requires your domain, DNS, TLS and SMTP configuration.

## Install

The default gateway listens on `127.0.0.1:8080`. Before running the local setup
helpers, ensure `shizuha.localhost` resolves to `127.0.0.1` on your machine. If
needed, add `127.0.0.1 shizuha.localhost` to your hosts file. You can also choose
your own hostname using the configuration below.

```sh
git clone https://github.com/shizuha-labs/deploy.git shizuha-platform
cd shizuha-platform
python3 selfhost.py validate --lock source-lock.json
python3 selfhost.py prepare --lock source-lock.json --output installation
python3 selfhost.py fetch --lock source-lock.json --output installation
cd installation
docker compose --project-name shizuha-selfhost --env-file secrets.env -f compose.json config --quiet
docker compose --project-name shizuha-selfhost --env-file secrets.env -f compose.json --profile runtime-build build
docker compose --project-name shizuha-selfhost --env-file secrets.env -f compose.json up -d --wait --wait-timeout 600
docker compose --project-name shizuha-selfhost --env-file secrets.env -f compose.json restart mail-server
python3 runtime_setup.py . --project shizuha-selfhost
python3 bootstrap_admin.py . --project shizuha-selfhost
python3 ../verify.py --url http://shizuha.localhost:8080
```

The administrator helper asks for a username, email and password, validates them,
and activates the first local administrator through ID's normal account lifecycle.
There is no preset administrator password. Preparation generates fresh secrets in
`installation/secrets.env` with owner-only permissions and refuses to overwrite an
existing installation. Keep that file private and back it up.

Open [your local platform](http://shizuha.localhost:8080). Sign in with the
administrator you just created. Use Admin to manage organizations and
membership, and ID to grant service access to other users.

| Application | Local path |
|---|---|
| Home and chat | `/` |
| Account and service access | `/id/` |
| Organizations and membership | `/admin/` |
| Agents | `/hive/` |
| Tasks | `/pulse/` |
| Messages | `/connect/` |
| Knowledge base | `/wiki/` |
| Maps | `/atlas/` |
| Notes | `/notes/` |
| Files | `/drive/` |
| Mail | `/mail/` |

The default mail domain is `example.org` for local use. Set `MAIL_DOMAIN` in
`secrets.env` before first startup if you have your own domain. The included
Stalwart server has no published SMTP ports; configure the appropriate external
mail edge before expecting internet delivery.

## Launch an agent

Save your inference provider key in an owner-readable file, outside this
repository. From the `installation` directory:

```sh
chmod 600 /path/to/provider-key.txt
python3 runtime_agent.py . --project shizuha-selfhost --operator YOUR_ADMIN \
  --username first-agent --model cortex/YOUR_MODEL \
  --provider-url https://inference.example.org/v1 \
  --provider-key-file /path/to/provider-key.txt
```

The helper asks for the local administrator password and stores the provider key
in the dedicated cluster. Open Hive to view the agent's readiness and activity.
The runtime and broker are built from the public runtime commit in the source lock.

## Optional operator routing

Home grants no special operator routing by default. If you need that role, set
`HOME_OPERATOR_USER_IDS` in `secrets.env` to the explicit numeric IDs of your own
installation's authorized operators. Set `HOME_OPERATOR_AGENT` to an agent you
have provisioned if you want a default conversation target. Rebuild the Home
frontend after changing these settings so its routing agrees with the backend.
Names and email addresses do not grant operator privileges.

## Use your own hostname

Choose the addresses before preparing a new installation:

```sh
python3 selfhost.py prepare --lock source-lock.json --output installation \
  --public-url https://platform.example.org \
  --storage-url https://files.example.org
```

Put your TLS reverse proxy in front of `127.0.0.1:8080`. Route the storage hostname
to `127.0.0.1:8333` without changing the path, query or Host header; signed downloads
use those values. Configure DNS and TLS for both hostnames. Databases and Redis
stay on the private Compose network.

## Backups and maintenance

Back up `secrets.env`, the ID signing keys and all persistent volumes, including
Home data, PostgreSQL, Redis, media, object storage, mail, Atlas and the agent cluster.
Restore-test backups before upgrading. Test a new source lock against a copy of
your installation first; reverting source alone does not reverse database
migrations. Do not remove volumes to repair an upgrade.

Inspect services from the installation directory:

```sh
docker compose --project-name shizuha-selfhost --env-file secrets.env -f compose.json ps
```

The included
`verify.py` checks HTTP health; it does not replace testing your sign-in, file,
mail and agent workflows. Never post your secrets file or fully expanded Compose
configuration in an issue.

## Release details

`source-lock.json` pins service/library/runtime commits and infrastructure image
digests. Service repositories contain history-isolated release snapshots;
intermediate private development history is excluded. See [RELEASE.md](RELEASE.md)
for release construction and verification. SCS, Books and Finance are planned
for later public releases.
