# Fleet deployment

The conductor reconciles a persistent desired count against labelled Docker containers or Kubernetes Pods. Its SQLite state uses a local single-writer file lock; run one controller against a state directory. The Kubernetes Deployment uses one replica and Recreate updates. A second reconciler sharing the same local state cannot launch duplicates. Do not put SQLite and its lock on a filesystem that does not support its locking semantics.

Workers are independent Pods with `restartPolicy: Never`. The conductor alone replaces them using fresh run ids. A Kubernetes Job's automatic retry could reuse an existing run's artifact directory, so this backend does not use Job retries. CPU, memory, and temporary storage have explicit requests and limits. Workers receive no service-account token, run without root privileges or additional capabilities, and have a read-only root filesystem. All mutable runtime files belong to bounded emptyDir volumes or persistent artifacts.

## Docker

Copy `fleet.docker.example.json` to a local configuration and set acceptance flags only after accepting the Minecraft EULA and isolated offline identities. The initial desired count is zero. Start the controller with the `serve` command and adjust the count through its authenticated dashboard/API. Increase `max_workers` and resource budgets only during deliberate capacity testing; the example ceiling is one.

The Docker daemon runs worker containers on the daemon host. Bind mount paths in configuration must exist on that host. If the controller runs in a container, mount its state and artifact paths at identical absolute paths inside it and on the daemon host. Access to the Docker socket gives the controller authority over the host; only trusted operators should control its API or configuration.

## Kubernetes

1. Build and publish/import `vibrato-worker:dev` and `vibrato-controller:dev` so cluster nodes can obtain them. For kind, use `kind load docker-image ... --name CLUSTER`.
2. Provision `vibrato-state`, `vibrato-artifacts`, and `vibrato-dataset` claims. `pvc.yaml` is the production starting point: select storage classes appropriate to your cluster, especially RWX shared storage for artifacts across nodes. Keep raw data on this claim independently of Pod lifetimes.
3. Create a `vibrato-dashboard-token` Secret in namespace `vibrato` containing a strong random `token` key. Do not commit its value.
4. Review `kubernetes.yaml` ConfigMap, acceptance flags, image names, measured worker ceiling, and resource budgets. Apply it after the namespace/storage/Secret exist.
5. Access the ClusterIP service through `kubectl -n vibrato port-forward service/vibrato-controller 8080:8080`, then use the token in the dashboard. Ingress/TLS is an operator deployment choice.

The controller uses its service account and namespace-scoped RBAC to list/create/delete workers and read logs. Workers cannot call the Kubernetes API. The worker isolation NetworkPolicy requires a CNI that enforces network policies; kind's default networking does not demonstrate enforcement. No worker Service is created, and game listeners bind Pod loopback.

For a world bank, provision a read-only claim, set `world_bank_pvc` and `world_ids` in the controller configuration, and explicitly enable full-copy fallback only when its disk cost is acceptable. The bank is mounted read-only into each worker.

## One-node kind validation

Use `kind-storage.yaml` instead of `pvc.yaml`, with node extra mounts `/conductor-state` and `/conductor-world-bank` supplied by the test cluster configuration. Create the host `controller`, `artifacts`, and `dataset` directories beforehand with ownership accessible to UID/GID 1000. These retained hostPath volumes are for a one-node local test; they do not establish a multi-node shared-storage deployment.

## Recovery and bounds

The conductor records an assignment and exclusive run id before creating a runtime. Labels/annotations permit adoption after the runtime accepted a create whose response was lost. Existing containers created but not started are started during recovery. Missing runtimes, unhealthy telemetry, crashed workers, and failed artifact checks cannot masquerade as completed dataset entries. Replacements preserve prior artifact and mutable world directories, use a fresh run id, and observe paced starts, failure backoff, and a repeated-failure circuit pause.

Healthy counts require live worker telemetry, not just a container or Pod in Running state. Resource pressure and delivery backpressure can inhibit further starts while existing workers continue to be monitored and stopped as needed. Desired zero drains the fleet. Manual stop reduces desired count; replace keeps it unchanged. Terminal runtime cleanup is bounded by `retained_workers` and requires a verified final artifact manifest; raw artifacts and worlds are governed separately by the storage retention policy.

The example configurations explicitly enable the short `director` and `get-dirt` tasks for initial diagnostics. Set `enabled_tasks` to the supported catalog task ids to broaden collection, and review `catalog_dir` if using a custom catalog. Long `nether` and `gamer` tasks have dedicated catalog budgets; the scheduler assignment budget overrides the general `seconds` fallback. A world bank directory/PVC plus `world_ids` restricts world selection. The coverage inventory still reports unsupported targets and observed gaps; enabled assignments do not count as observed outcomes.

Artifact spooling (`artifacts_dir`, shared with workers) is separate from verified central delivery (`central_dir`). The Kubernetes examples mount a dedicated dataset claim so raw captures cannot fill the SQLite state claim. Delivery retry, maximum buffered bytes, and separate spool/central diagnostic/failure retention are explicit in the example. Active runs and undelivered evidence are protected from retention. A delivery backlog inhibits new starts without stopping observation of active workers.
