# Rollout step 8: in a deterministic env SELECT comes only from table_readers.
#   - a table's readers are the union of every matching entry (exact table and
#     its catalog.schema.*), and a wildcard reaches every governed table;
#   - agents' acl_groups, policy targets and groups never grant anything;
#   - nothing is granted to account users, and a reader outside the tiers and
#     raw_exempt_principals is refused;
#   - new SELECT still waits for a coverage check pass; removing an entry
#     revokes what it granted without one;
#   - an unset table_readers never silently revokes grants already applied.

mock_provider "databricks" {
  alias = "account"
}
mock_provider "databricks" {
  alias = "workspace"
}
mock_provider "time" {}

variables {
  databricks_account_id     = "account"
  databricks_client_id      = "service-principal"
  databricks_client_secret  = "secret"
  databricks_workspace_host = "https://example.invalid"
  sql_warehouse_id          = "warehouse"
  masking_sql_file          = "tests/fixtures/mask_email.sql"
  deploy_masking_script     = "tests/fixtures/noop_deploy_masking.py"
  auth_file                 = "tests/.tmp/readers/data_access/auth.auto.tfvars"
  coverage_gate_file        = "tests/.tmp/readers/data_access/.coverage_gate.json"
  coverage_ddl_file         = "tests/.tmp/readers/ddl/_fetched.sql"
  governance_mode           = "deterministic"
  access_tier_groups        = ["raw", "partial", "full"]
  raw_exempt_principals     = ["etl"]
  # Agent acl_groups and groups that must never turn into SELECT.
  groups                 = { raw = {}, partial = {}, full = {}, analysts = {} }
  uc_tables              = ["cat.sch.orders", "cat.sch.customers"]
  admin_uc_tables        = ["cat.sch.orders", "cat.sch.customers"]
  table_agents           = { "cat.sch.orders" = ["agent"], "cat.sch.customers" = ["agent"] }
  genie_space_acl_groups = { agent = ["analysts", "full"] }
  # Governed only through the governed-table list: the wildcard still reaches it.
  governed_tables = ["cat.sch.payments"]
  table_readers = {
    "cat.sch.customers" = ["partial"]
    "cat.sch.*"         = ["raw"]
    "cat.sch.orders"    = ["etl", "raw"]
  }
  tag_assignments = [
    { entity_type = "columns", entity_name = "cat.sch.customers.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
  ]
  fgac_policies = [
    {
      name              = "gr_mask_cat_email_partial__string__0a1b2c3d_partial"
      policy_type       = "POLICY_TYPE_COLUMN_MASK"
      catalog           = "cat"
      to_principals     = ["partial"]
      except_principals = ["raw", "etl"]
      match_condition   = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')"
      match_alias       = "masked_col"
      function_name     = "gr_mask_email_partial__string__0a1b2c3d_partial"
      function_catalog  = "cat"
      function_schema   = "sch"
    },
    {
      name              = "gr_mask_cat_email_partial__string__0a1b2c3d_full"
      policy_type       = "POLICY_TYPE_COLUMN_MASK"
      catalog           = "cat"
      to_principals     = ["account users"]
      except_principals = ["raw", "partial", "etl"]
      match_condition   = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')"
      match_alias       = "masked_col"
      function_name     = "gr_mask_email_partial__string__0a1b2c3d_full"
      function_catalog  = "cat"
      function_schema   = "sch"
    },
  ]
}

run "setup_inputs" {
  module {
    source = "../../roots/data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/readers/ddl/_fetched.sql"                = "CREATE TABLE cat.sch.customers (\n  id BIGINT,\n  email STRING\n);\n"
      "tests/.tmp/readers/data_access/.coverage_gate.json" = null
    }
  }
}

# Without a coverage check pass, new SELECT is withheld (nothing planned).
run "new_select_waits_for_the_coverage_check" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  assert {
    condition     = length(databricks_grant.table_access) == 0
    error_message = "no SELECT may be planned without a passing coverage check"
  }
  assert {
    condition = toset(output.withheld_table_grants.grants) == toset([
      "cat.sch.customers|partial", "cat.sch.customers|raw", "cat.sch.orders|etl", "cat.sch.orders|raw", "cat.sch.payments|raw",
    ])
    error_message = "every table_readers grant must be withheld, and only those"
  }
}

run "write_passing_gate" {
  module {
    source = "../../roots/data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/readers/ddl/_fetched.sql"                = "CREATE TABLE cat.sch.customers (\n  id BIGINT,\n  email STRING\n);\n"
      "tests/.tmp/readers/data_access/.coverage_gate.json" = jsonencode({ status = "pass", fingerprint = run.new_select_waits_for_the_coverage_check.coverage_gate_inputs.fingerprint, refreshed_at = "@NOW@" })
    }
  }
}

run "select_only_from_table_readers" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  assert {
    condition = toset(keys(databricks_grant.table_access)) == toset([
      "cat.sch.customers|partial", "cat.sch.customers|raw", "cat.sch.orders|etl", "cat.sch.orders|raw", "cat.sch.payments|raw",
    ])
    error_message = "SELECT must be exactly the union of matching table_readers entries (exact table and catalog.schema.*)"
  }
  assert {
    condition     = toset(keys(databricks_grant.catalog_access)) == toset(["cat|etl", "cat|partial", "cat|raw"]) && toset(keys(databricks_grant.schema_access)) == toset(["cat.sch|etl", "cat.sch|partial", "cat.sch|raw"])
    error_message = "USE_CATALOG / USE_SCHEMA go only to readers granted SELECT there"
  }
  assert {
    condition = length([
      for principal in concat(
        [for grant in databricks_grant.table_access : grant.principal],
        [for grant in databricks_grant.catalog_access : grant.principal],
        [for grant in databricks_grant.schema_access : grant.principal],
      ) : principal if contains(["account users", "analysts", "full"], principal)
    ]) == 0
    error_message = "agents' acl_groups, policy targets (account users) and groups must never be granted anything"
  }
  assert {
    condition     = join(",", output.access_config.readers["cat.sch.customers"]) == "partial,raw" && join(",", output.access_config.readers["cat.sch.payments"]) == "raw"
    error_message = "resolved readers per table must be printed as the union"
  }
}

# Staged policy phases (step 6) withhold new SELECT even with a pass, and keep
# every grant already applied: withholding never revokes.
run "staged_phase_withholds_new_select_and_keeps_applied" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  expect_failures = []
  variables {
    withhold_new_table_grants = true
    applied_table_grants      = ["cat.sch.orders|raw"]
  }
  assert {
    condition     = toset(keys(databricks_grant.table_access)) == toset(["cat.sch.orders|raw"])
    error_message = "a staged phase plans only the SELECT already applied"
  }
  assert {
    condition = toset(output.withheld_table_grants.grants) == toset([
      "cat.sch.customers|partial", "cat.sch.customers|raw", "cat.sch.orders|etl", "cat.sch.payments|raw",
    ])
    error_message = "every new table_readers grant is withheld until the grant phase"
  }
}

run "reader_outside_the_tiers_is_refused" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  variables {
    table_readers = { "cat.sch.orders" = ["analysts"] }
  }
  expect_failures = [output.access_config]
}

run "account_users_is_never_a_reader" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  variables {
    # Even when named as a tier.
    access_tier_groups = ["raw", "partial", "account users"]
    table_readers      = { "cat.sch.*" = ["account users"] }
  }
  expect_failures = [output.access_config]
}

run "invalid_key_is_refused" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  variables {
    table_readers = { "cat.*.orders" = ["raw"] }
  }
  expect_failures = [output.access_config]
}

# Removing an entry revokes the SELECT it granted, with no coverage check
# pass needed (the gate file is now stale for the new inputs).
run "removed_entry_revokes_without_a_pass" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  variables {
    applied_table_grants = ["cat.sch.orders|etl"]
    table_readers        = {}
  }
  assert {
    condition     = length(databricks_grant.table_access) == 0 && length(databricks_grant.catalog_access) == 0
    error_message = "an explicit empty table_readers must revoke every grant it made"
  }
}

# Upgrade safety: an env whose state holds SELECT grants must not lose them
# because table_readers was never set.
run "unset_table_readers_refuses_to_revoke_applied_grants" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  variables {
    applied_table_grants = ["cat.sch.orders|raw"]
    table_readers        = null
  }
  expect_failures = [output.access_config]
}

run "unset_table_readers_grants_nothing_on_a_fresh_env" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  variables {
    table_readers = null
  }
  assert {
    condition     = length(databricks_grant.table_access) == 0 && length(output.withheld_table_grants.grants) == 0
    error_message = "no table_readers means GenieRails grants no SELECT"
  }
}

# Legacy envs ignore table_readers entirely.
run "legacy_ignores_table_readers" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  variables {
    governance_mode = "legacy"
  }
  assert {
    condition     = !contains(output.withheld_table_grants.grants, "cat.sch.orders|etl") && !contains(output.withheld_table_grants.grants, "cat.sch.payments|raw")
    error_message = "legacy grants must not come from table_readers"
  }
}

# Upgrade: an env whose state already holds SELECT (an earlier apply) gets
# table_readers proposed for exactly those readers; nothing is revoked. (With
# no pass for the new inputs the plan fails closed instead: see the precondition
# on kept grants with unrecorded protection.)
run "proposed_readers_gate_inputs" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  plan_options {
    mode = refresh-only
  }
  variables {
    applied_table_grants = ["cat.sch.customers|partial", "cat.sch.orders|raw"]
    table_readers = {
      "cat.sch.customers" = ["partial"]
      "cat.sch.orders"    = ["raw"]
    }
  }
}

run "proposed_readers_gate_passes" {
  module {
    source = "../../roots/data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/readers/ddl/_fetched.sql"                = "CREATE TABLE cat.sch.customers (\n  id BIGINT,\n  email STRING\n);\n"
      "tests/.tmp/readers/data_access/.coverage_gate.json" = jsonencode({ status = "pass", fingerprint = run.proposed_readers_gate_inputs.coverage_gate_inputs.fingerprint, refreshed_at = "@NOW@" })
    }
  }
}

run "proposed_readers_keep_every_applied_grant" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  variables {
    applied_table_grants = ["cat.sch.customers|partial", "cat.sch.orders|raw"]
    table_readers = {
      "cat.sch.customers" = ["partial"]
      "cat.sch.orders"    = ["raw"]
    }
  }
  assert {
    condition     = toset(keys(databricks_grant.table_access)) == toset(["cat.sch.customers|partial", "cat.sch.orders|raw"])
    error_message = "every applied grant the proposal covers must stay planned (no surprise revocation), and nothing else"
  }
}
