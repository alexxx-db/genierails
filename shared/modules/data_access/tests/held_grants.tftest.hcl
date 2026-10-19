# make policy-rebind's first phase holds back the grants deleted principals
# lost (held_access_grants), so the re-pointed protection is applied and
# checked live before any of them is created again. Only the listed grants
# leave the plan; every other grant stays.

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
  auth_file                 = "tests/.tmp/held/data_access/auth.auto.tfvars"
  coverage_gate_file        = "tests/.tmp/held/data_access/.coverage_gate.json"
  coverage_ddl_file         = "tests/.tmp/held/ddl/_fetched.sql"
  groups                    = { analysts = {}, auditors = {} }
  uc_tables                 = ["cat.sch.customers"]
  admin_uc_tables           = ["cat.sch.customers"]
  tag_assignments = [{
    entity_type = "columns"
    entity_name = "cat.sch.customers.email"
    tag_key     = "gr_treatment"
    tag_value   = "mask_email"
  }]
  fgac_policies = [{
    name             = "mask_email"
    policy_type      = "POLICY_TYPE_COLUMN_MASK"
    catalog          = "cat"
    to_principals    = ["analysts", "auditors"]
    match_condition  = "hasTagValue('gr_treatment', 'mask_email')"
    match_alias      = "email"
    function_name    = "mask_email"
    function_catalog = "cat"
    function_schema  = "sch"
  }]
}

run "setup_inputs" {
  module {
    source = "../../roots/data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/held/ddl/_fetched.sql" = "CREATE TABLE cat.sch.customers (\n  id BIGINT,\n  email STRING\n);\n"
    }
  }
}

run "every_grant_without_a_hold" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  assert {
    condition     = toset(keys(databricks_grant.catalog_access)) == toset(["cat|analysts", "cat|auditors"])
    error_message = "both catalog grants are planned"
  }
  assert {
    condition     = toset(keys(databricks_grant.schema_access)) == toset(["cat.sch|analysts", "cat.sch|auditors"])
    error_message = "both schema grants are planned"
  }
  assert {
    condition     = toset(keys(databricks_grant.terraform_sp_manage_catalog)) == toset(["cat"])
    error_message = "the deployer's catalog grant is planned"
  }
}

run "held_grants_leave_the_plan" {
  command = plan
  providers = {
    databricks.account   = databricks.account
    databricks.workspace = databricks.workspace
    time                 = time
  }
  variables {
    held_access_grants = ["catalog_access|cat|analysts", "schema_access|cat.sch|analysts", "terraform_sp_manage_catalog|cat"]
  }
  assert {
    condition     = toset(keys(databricks_grant.catalog_access)) == toset(["cat|auditors"])
    error_message = "only the held catalog grant leaves the plan"
  }
  assert {
    condition     = toset(keys(databricks_grant.schema_access)) == toset(["cat.sch|auditors"])
    error_message = "only the held schema grant leaves the plan"
  }
  assert {
    condition     = length(databricks_grant.terraform_sp_manage_catalog) == 0
    error_message = "the held deployer grant leaves the plan"
  }
  assert {
    condition     = length(databricks_policy_info.policies) == 1
    error_message = "holding grants never drops a policy"
  }
}
