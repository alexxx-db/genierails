# A business group's USE_CATALOG and USE_SCHEMA open with its SELECT, in the
# grant phase. Before it (create, tighten, switch, restore, retire, abandon) a
# deterministic plan keeps the USE grants already applied and creates none, so
# a first deploy's create phase plans only the deployer's grant, the functions
# and the policies (a rebind holds back what it would create instead).

mock_provider "databricks" {}
mock_provider "null" {}
mock_provider "time" {}

variables {
  env_dir                   = "tests/.tmp/use_grants/data_access"
  databricks_account_id     = "account"
  databricks_client_id      = "service-principal"
  databricks_client_secret  = "secret"
  databricks_workspace_host = "https://example.invalid"
  databricks_workspace_id   = "123"
  sql_warehouse_id          = "warehouse"
  governance_mode           = "deterministic"
  access_tier_groups        = ["raw", "partial", "full"]
  groups                    = { raw = {}, partial = {}, full = {} }
  uc_tables                 = ["cat.sch.orders"]
  table_readers             = { "cat.sch.orders" = ["raw", "partial", "full"] }
  fgac_policies = [
    { name = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_partial", function_catalog = "cat", function_schema = "sch" },
    { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
  ]
  # The create render of a first deploy: policies, no treatment tag yet.
  tag_assignments = []
}

run "fresh_env" {
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/use_grants/data_access/masking_functions.sql" = "SELECT 1;\n"
      "tests/.tmp/use_grants/data_access/terraform.tfstate"     = null
    }
  }
}

run "first_deploy_create_plans_no_business_use" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage = "create"
  }
  assert {
    condition     = length(module.data_access.catalog_grant_resource_keys) == 0 && length(module.data_access.schema_grant_resource_keys) == 0
    error_message = "the create phase of a first deploy must plan no USE_CATALOG or USE_SCHEMA for a business group"
  }
  assert {
    condition     = length(module.data_access.table_grant_resource_keys) == 0
    error_message = "the create phase must plan no SELECT"
  }
}

run "tighten_plans_no_business_use" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage = "tighten"
  }
  assert {
    condition     = length(module.data_access.catalog_grant_resource_keys) + length(module.data_access.schema_grant_resource_keys) == 0
    error_message = "tighten must plan no new USE"
  }
}

run "switch_plans_no_business_use" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage = "switch"
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
    ]
  }
  assert {
    condition     = length(module.data_access.catalog_grant_resource_keys) + length(module.data_access.schema_grant_resource_keys) == 0
    error_message = "switch must plan no new USE: it opens with SELECT in the grant phase"
  }
}

run "restore_plans_no_business_use" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage = "restore"
  }
  assert {
    condition     = length(module.data_access.catalog_grant_resource_keys) + length(module.data_access.schema_grant_resource_keys) == 0
    error_message = "restore must plan no new USE"
  }
}

run "retire_plans_no_business_use" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage = "retire"
  }
  assert {
    condition     = length(module.data_access.catalog_grant_resource_keys) + length(module.data_access.schema_grant_resource_keys) == 0
    error_message = "retire must plan no new USE"
  }
}

run "abandon_plans_no_business_use" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage = "abandon"
  }
  assert {
    condition     = length(module.data_access.catalog_grant_resource_keys) + length(module.data_access.schema_grant_resource_keys) == 0
    error_message = "abandon must plan no new USE"
  }
}

run "grant_opens_use_with_select" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage = "grant"
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
    ]
  }
  assert {
    condition     = toset(module.data_access.catalog_grant_resource_keys) == toset(["cat|raw", "cat|partial", "cat|full"]) && toset(module.data_access.schema_grant_resource_keys) == toset(["cat.sch|raw", "cat.sch|partial", "cat.sch|full"])
    error_message = "the grant phase creates the tier groups' USE_CATALOG and USE_SCHEMA"
  }
}

run "rebind_is_not_withheld" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage       = "rebind"
    held_access_grants = ["catalog_access|cat|full"]
  }
  assert {
    condition     = toset(module.data_access.catalog_grant_resource_keys) == toset(["cat|raw", "cat|partial"]) && length(module.data_access.schema_grant_resource_keys) == 3
    error_message = "a rebind holds back exactly held_access_grants, so its access phase can grant lost USE again"
  }
}

run "preview_plans_use" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage = "preview"
  }
  assert {
    condition     = length(module.data_access.catalog_grant_resource_keys) == 3 && length(module.data_access.schema_grant_resource_keys) == 3
    error_message = "a preview plan shows every USE grant"
  }
}

run "legacy_mode_is_not_withheld" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    governance_mode = "legacy"
    table_readers   = null
    policy_stage    = "create"
  }
  assert {
    condition     = length(module.data_access.catalog_grant_resource_keys) > 0
    error_message = "legacy mode is never staged, so its USE grants are not withheld"
  }
}

# Live USE grants of raw (an earlier release): kept in every phase; partial
# and full, new readers, wait for grant.
run "applied_use_grants" {
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/use_grants/data_access/masking_functions.sql" = "SELECT 1;\n"
      "tests/.tmp/use_grants/data_access/terraform.tfstate"     = jsonencode({ version = 4, resources = [{ module = "module.data_access", mode = "managed", type = "databricks_grant", name = "catalog_access", provider = "provider[\"registry.terraform.io/databricks/databricks\"].workspace", instances = [{ index_key = "cat|raw", schema_version = 0, attributes = { catalog = "cat", principal = "raw" } }] }, { module = "module.data_access", mode = "managed", type = "databricks_grant", name = "schema_access", provider = "provider[\"registry.terraform.io/databricks/databricks\"].workspace", instances = [{ index_key = "cat.sch|raw", schema_version = 0, attributes = { schema = "cat.sch", principal = "raw" } }] }] })
    }
  }
}

run "create_keeps_applied_use_and_withholds_new" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage = "create"
  }
  assert {
    condition     = toset(module.data_access.catalog_grant_resource_keys) == toset(["cat|raw"]) && toset(module.data_access.schema_grant_resource_keys) == toset(["cat.sch|raw"])
    error_message = "create keeps the applied USE grants (never revokes them) and plans no new one"
  }
}
