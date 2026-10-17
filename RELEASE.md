# Maintaining platform releases

Export only the committed `public/self-host` subtree of the private deployment
repository. Production charts, host configuration, backup scripts, runtime data
and development history are outside this public export boundary. Construct a
tree-only import whose parent is the previous publish tip, then a merge on that
lane. Never merge a development branch into publish. Scan all reachable history
and the entire final tree; bind review evidence to exact commit and tree hashes.

## Source contract

Release maintainers provide `source-lock.json`, schema 1, containing exactly
`id`, `hive`, `pulse`, `connect`, `wiki`, `atlas`, `admin`, `notes`, `drive`,
`mail`, `home`, `packages` and `runtime`. Each entry has
`url` (an anonymous HTTPS GitHub URL in `shizuha-labs`, ending in `.git`) and
`commit` (the full, approved publish commit SHA). The lock also requires `images`
entries for `postgres`, `redis`, `nginx`, `object_store`, `mail_server` and `k3s`, each a public
registry reference pinned with `@sha256:<64 hex characters>`. Validate these
images with an anonymous pull on every supported architecture before releasing the lock.
The S3 implementation is [SeaweedFS mini](https://github.com/seaweedfs/seaweedfs#docker),
with a Python bootstrap that creates the application buckets. Release CI must
verify anonymous retrieval of every pinned infrastructure image.
For example, the source-entry shape is:

```json
{"schema": 1, "sources": {"wiki": {"url": "https://github.com/shizuha-labs/wiki.git", "commit": "FULL_REVIEWED_PUBLISH_SHA"}}}
```

The abbreviated example deliberately does not validate. All thirteen source entries, real SHAs and six image digests are mandatory. `packages` must include the reviewed `shizuha-auth` and
`shizuha-storage` Python distributions. Frontend lockfiles must resolve all
`@shizuha/*` packages, including UI/editor/Atlas, anonymously from public sources.
A private cache URL inside a lockfile is a release blocker, even when the build
sets the npm registry to a public URL. Source locks identify the public snapshot,
never a private development commit. The publisher must verify these SHAs against
its scan receipts; this installer does not replace that security review.


Private rehearsal packages must include the committed installer's own
`source-lock.json`, byte-equivalent as JSON to the bundle lock, and a
`candidate-manifest.json` with the exact source commit/tree for each lock entry
and the installer commit/subtree/tree. The CI bundle verifier recomputes Git
tree identities from exported file bytes, executable bits and symlinks before
running any installer code. Export only Git archives of the reviewed commits;
do not substitute a workspace or modify the acceptance scripts in a held run.

## Release acceptance

Before declaring a release deployable, Origin CI must build and exercise Linux
amd64 using only the locked public sources and public dependency registries.
Production service image builds retain their separate multi-architecture gates. Run
the [installation commands](README.md#install) in an isolated environment with no host credentials. Record source SHAs,
image digests, migration exits, service health, and the browser version. Fail the
release if any package or image requires internal DNS or authentication.

The included `verify.py` proves UI documents and health endpoints respond; it is
not the authenticated acceptance suite. Exercise real ID and organization APIs,
verify Home's authenticated dashboard and tenant isolation, create/read a Pulse task, Connect message, Wiki page, Atlas map, Note, Drive file
and Mail message, and verify persistence across an application restart. Use a
real browser for sign-in, application rendering, Notes workflows and Wiki
editor typing, save and reload. Test a second user
and organization to prove isolation; anonymous access to private objects must be
rejected. Exercise uploads/downloads and worker jobs. Verify configuration uses
the installation origin rather than a hosted Shizuha service. Hive's agent
runtime and provider setup need an explicit public runtime release and a separate
agent-launch journey; control-plane health alone does not establish that agents
can run. Capture failures as release blockers, never waive them with green
health checks.

Other release checks: environment-driven
host/CSRF/script-prefix configuration in each released service; public frontend dependency lockfiles;
external email and object-storage browser download configuration; organization bootstrap through the included Admin service; and Hive's public agent runtime. This file records
those requirements; only completed, source-bound acceptance proves a release.

Run the installer contract tests with:

```sh
python3 -m unittest discover -s . -p 'test_*.py'
```

## Release preparation for shared frontend libraries

The npm `@shizuha/ui` source lives in the platform's `packages/shizuha-ui`
directory. A repository with a similar name must not be assumed to contain that
library. Export reviewed Python auth/storage and npm UI/editor sources into the
`platform-packages` publish repository. The reusable `@shizuha/atlas` library is
a distinct dependency from the standalone Atlas service.

Build and `npm pack` the reviewed libraries through the release lane. Feed those
artifacts to the vendor tool for each service frontend, including Atlas:

```sh
python3 vendor_frontend.py --frontend candidate/wiki/frontend --artifacts reviewed-npm-artifacts
```

This writes checked-in `vendor/*.tgz`, their SHA-256 manifest, and the resulting
`package.json` / `package-lock.json`. It replaces private scoped dependencies
with local tarballs and rewrites only known npm proxy URLs to their public
registry equivalents while preserving locked versions and integrity records.
It fails on unknown dependency hosts or private transitive dependency edges.
Scan unpacked tarballs, source, license files, generated assets and every final
publish commit. Review and commit these artifacts before publishing the source
lock. End users run `npm ci`; they do not regenerate locks or fetch private
packages. Release evidence must include an actual clean `npm ci` and frontend
build, not only the vendor tool's success.

## Dependency audit scope

The 2026-10-03 review found no production npm advisories in the approved source
candidates. Some Tailwind 3 build toolchains retain the unfixed
[braces stack-exhaustion advisory](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm).
The affected parser runs against repository-controlled build patterns, not
platform users' requests. This is documented in the affected repositories'
`frontend/DEPENDENCY_SECURITY.md`; it is not hidden with an audit ignore.
Build only reviewed source and do not expose development/watch servers. Refresh
both production and development audits for each later release.
