Prod environment overlay placeholder. Layering: workload/base (environment-neutral service roots
and charts) is composed by workload/environments/<env> (per-environment values, Ingress, SOPS
Secrets) and rendered by the sync root workload/eu-north-1-<env>. The prod cluster size is
undefined. When it is decided, mirror the staging overlay with the larger instance type and the
prod values, and add the prod FluxInstance ConfigMap on the management side.
