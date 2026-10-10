mock_provider "databricks" { alias = "account" }
mock_provider "databricks" { alias = "workspace" }
mock_provider "time" {}

# The same governed tables, groups, agent ACLs and tier policies (the full one
# TO account users) planned in both modes.
variables {
  databricks_account_id     = "account"
  databricks_client_id      = ""
  databricks_client_secret  = "secret"
  databricks_workspace_host = "https://example.invalid"
  sql_warehouse_id          = "warehouse"
  masking_sql_file          = "tests/fixtures/mask_email.sql"
  deploy_masking_script     = "tests/fixtures/noop_deploy_masking.py"
  auth_file                 = "tests/fixtures/missing-auth.auto.tfvars"
  coverage_gate_file        = "tests/fixtures/missing.json"
  coverage_ddl_file         = "tests/fixtures/mask_email.sql"
  groups                    = { raw = {}, partial = {}, full = {} }
  uc_tables                 = ["cat.sch.orders"]
  admin_uc_tables           = ["cat.sch.orders"]
  genie_space_acl_groups    = { agent = ["partial"] }
  tag_assignments = [
    { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
  ]
  fgac_policies = [
    {
      name              = "gr_mask_cat_email_partial__string__0a1b2c3d_partial"
      policy_type       = "POLICY_TYPE_COLUMN_MASK"
      catalog           = "cat"
      to_principals     = ["partial"]
      except_principals = ["raw"]
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
      except_principals = ["raw", "partial"]
      match_condition   = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')"
      match_alias       = "masked_col"
      function_name     = "gr_mask_email_partial__string__0a1b2c3d_full"
      function_catalog  = "cat"
      function_schema   = "sch"
    },
  ]
}

# uc_tables governs and never grants: deterministic governance creates the
# masks but no business grant, so nothing targets account users either.
run "deterministic_governs_without_granting" {
  command   = plan
  providers = { databricks.account = databricks.account, databricks.workspace = databricks.workspace, time = time }
  variables { governance_mode = "deterministic" }
  assert {
    condition     = length(databricks_grant.catalog_access) == 0 && length(databricks_grant.schema_access) == 0 && length(databricks_grant.table_access) == 0
    error_message = "deterministic governance must grant no USE_CATALOG, USE_SCHEMA or SELECT"
  }
  assert {
    condition     = length(databricks_policy_info.policies) == 2 && length(databricks_entity_tag_assignment.treatment) == 1
    error_message = "the tier policies and the treatment tag are still created"
  }
}

# Legacy envs keep their grants unchanged (including the one to account users
# that a policy target implies), so they plan exactly as before. SELECT waits
# for a coverage check pass, which this plan has none of.
run "legacy_grants_are_unchanged" {
  command   = plan
  providers = { databricks.account = databricks.account, databricks.workspace = databricks.workspace, time = time }
  assert {
    condition     = contains(keys(databricks_grant.catalog_access), "cat|account users") && length(databricks_grant.schema_access) == 4
    error_message = "legacy access grants must not change"
  }
}
