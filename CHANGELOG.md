# Changelog

Notable changes to intune-cmdb-sync. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). No release has been
tagged yet, so everything is under Unreleased. Entries start on 2026-10-14;
for earlier history see `git log`.

## [Unreleased]

### 2026-10-16

#### Changed
- With `SNOW_CREATE_MISSING_MODELS=true`, a model is created only when its
  manufacturer resolved to a `core_company` record, and it is linked to that
  record. A model whose manufacturer matches nothing is left unset and named in
  a run-report warning; the next run creates it once the manufacturer exists or
  is aliased. Lookup of existing models is unchanged, and a failed create is
  still never retried within a run.

#### Added
- `SNOW_CREATE_MISSING_MODELS` now reaches deployed jobs: `createMissingModels`
  in `deploy/azure` (set from `deploy.sh`, strict `true`/`false`, default
  `false`) and `create_missing_models` in `deploy/aws`. Before this, a deployed
  job never created models, whatever the local `.env` said.
  `SNOW_CREATE_MISSING_MANUFACTURERS` is deliberately not passed through;
  deployments keep its default, `false`.

#### Documentation
- `CLAUDE.md` records the PROD (dpsnow) `core_company` findings: the
  integration account can insert rows but not write `name` or `manufacturer`,
  and the table holds legal entities rather than vendors.
- `mapping-overrides.prod.json` is gitignored, so PROD can carry its own
  overrides (aliases, field drops) separate from the dev file.

### 2026-10-14

#### Added
- Mac RAM from an Intune macOS custom attribute (`INTUNE_MAC_RAM_ATTRIBUTE`,
  reported in MB), since Graph reports 0 for Macs. The attribute script is
  included.
- Mac `cpu_name` from an Intune macOS custom attribute
  (`INTUNE_MAC_CPU_ATTRIBUTE`).
- `INTUNE_FETCH_HARDWARE_DETAIL` fetches RAM, ethernet MAC and CPU
  architecture through Graph `$batch`, and passes through both deployments.
- `SNOW_SOURCE_RECENCY` (`--source-recency run_time` to backfill a new field)
  and `manufacturer_aliases` / `model_aliases` in mapping overrides.
- Run reports carry `sent_values` and `ire_detail` per device.

#### Fixed
- CIs are no longer linked to a reference record ServiceNow created without
  its name. The run report warns and names the record instead.
