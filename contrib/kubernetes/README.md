# Kubernetes example

Kustomize manifests for running one shared deployment of `ghcr.io/dcode/mcp-google-tasks` on
Kubernetes. They are an example to copy and adapt, not a maintained chart: every value marked
`example` needs editing first. Nothing here has been applied to a real cluster yet (see
[ROADMAP.md](../../docs/ROADMAP.md)).

| Directory | Auth mode | Use it for | State |
|---|---|---|---|
| [base/](base/) | `resource-server` | Obot Remote entry with Static OAuth (multi user) | None |
| [oauth-proxy/](oauth-proxy/) | `oauth-proxy` | MCP clients that connect directly and do OAuth themselves | Encrypted files on a PVC |

Both create the namespace `google-tasks` with a Deployment, a ClusterIP Service on port 80 and
an Ingress for `tasks.example.com` that terminates TLS. Read
[deployment-obot.md](../../docs/deployment-obot.md) for the Google and Obot side, and
[configuration.md](../../docs/configuration.md) for every variable.

## resource-server (Obot)

1. In [base/kustomization.yaml](base/kustomization.yaml), set `MCP_BASE_URL` and
   `GOOGLE_OAUTH_CLIENT_ID` (the client Obot uses), and pin the image tag.

2. In [base/ingress.yaml](base/ingress.yaml), set the host, the TLS secret and your ingress
   class, and add your controller's annotations for a long read timeout and unbuffered
   responses (streamable HTTP keeps server-sent event streams open).

3. Apply and check:

   ```bash
   kubectl apply -k contrib/kubernetes/base
   kubectl -n google-tasks rollout status deployment/google-tasks
   curl -fsS https://tasks.example.com/health
   curl -fsS https://tasks.example.com/.well-known/oauth-protected-resource/mcp
   ```

4. Add the Remote catalog entry in Obot with URL `https://tasks.example.com/mcp` and Static
   OAuth ([deployment-obot.md](../../docs/deployment-obot.md#3-add-the-remote-catalog-entry)).

The host must be public: Obot rejects private and loopback addresses for Remote entries by
default, so the in-cluster Service address does not work even when Obot runs in the same
cluster.

## oauth-proxy

The overlay switches the mode, keeps tokens in the `filetree` backend on a 1 GiB
PersistentVolumeClaim mounted at `/data`, and reads the secrets from an untracked env file:

```bash
cp contrib/kubernetes/oauth-proxy/secrets.env.example contrib/kubernetes/oauth-proxy/secrets.env
# Fill in secrets.env; git ignores it.
kubectl apply -k contrib/kubernetes/oauth-proxy
```

Set `MCP_ALLOWED_CLIENT_REDIRECT_URIS` in
[oauth-proxy/kustomization.yaml](oauth-proxy/kustomization.yaml) to the clients you expect,
and register `https://tasks.example.com/auth/callback` with Google. Keep `JWT_SIGNING_KEY` and
`STORAGE_ENCRYPTION_KEY` stable: changing either signs everyone out. If you manage secrets
another way (External Secrets, Sealed Secrets), drop the `secretGenerator` and create a Secret
named `google-tasks-secrets` with the same keys.

## Choices worth knowing

- **One replica.** Streamable HTTP sessions live in the pod's memory, so a second replica
  would receive requests for sessions it never saw. Scaling out needs session affinity on the
  `Mcp-Session-Id` header and, in `oauth-proxy` mode, `TOKEN_STORAGE_BACKEND=redis` instead of
  the volume. The overlay uses the `Recreate` strategy because its volume is ReadWriteOnce.
- **Locked-down pod.** Runs as the image's uid 10001, with all capabilities dropped, no
  privilege escalation, the `RuntimeDefault` seccomp profile, no service account token and a
  read-only root filesystem (`/tmp` and `/data` are the only writable mounts). The namespace
  enforces the `restricted` Pod Security Standard.
- **Probes.** Startup, liveness and readiness all use `GET /health`, which answers without
  authentication. It shows the process serves HTTP, not that Google is reachable.
- **Configuration.** Non-secret settings come from a generated ConfigMap and secrets from an
  optional Secret, both through `envFrom`; the generated names carry a content hash, so editing
  either rolls the Deployment.

Render without applying to review the output:

```bash
kubectl kustomize contrib/kubernetes/base
```
