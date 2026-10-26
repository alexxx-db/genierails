# The Terraform backstop of staged policy changes (rollout step 6): outside
# the staged controller's phases, a deterministic plan may not remove,
# re-principal or swap the function of a live mask policy, nor retag a
# governed column, so a one-shot apply can never drop the old mask before the
# column moves to the new one. The live state is the root's own state file,
# written here in the shape Terraform keeps it.

mock_provider "databricks" {}
mock_provider "null" {}
mock_provider "time" {}

variables {
  env_dir                   = "tests/.tmp/staged/data_access"
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
  fgac_policies = [
    { name = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_partial", function_catalog = "cat", function_schema = "sch" },
    { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
  ]
  tag_assignments = [
    { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
  ]
}

run "setup" {
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/staged/data_access/masking_functions.sql" = "SELECT 1;\n"
      "tests/.tmp/staged/data_access/terraform.tfstate"     = null
    }
  }
}

# Nothing applied yet: the first apply creates policies and tags together
# (the module orders the tags after the policies).
run "gate_inputs" {
  command = plan
}

run "fresh_env_applies_in_one_plan" {
  command = plan
  variables {
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__9f8e7d6c" },
    ]
  }
}

run "live_state" {
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/staged/data_access/masking_functions.sql" = "SELECT 1;\n"
      "tests/.tmp/staged/data_access/terraform.tfstate"     = jsonencode({ version = 4, outputs = { coverage_gate = { value = { status = "pass", fingerprint = "applied", deployment_binding = run.gate_inputs.coverage_gate_inputs.deployment_binding } } }, resources = [{ module = "module.data_access", mode = "managed", type = "databricks_policy_info", name = "policies", provider = "provider[\"registry.terraform.io/databricks/databricks\"].workspace", instances = [{ index_key = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", schema_version = 0, attributes = { to_principals = ["partial"], except_principals = ["raw"], column_mask = { function_name = "cat.sch.gr_mask_email_partial__string__0a1b2c3d_partial" } } }, { index_key = "gr_mask_cat_email_partial__string__0a1b2c3d_full", schema_version = 0, attributes = { to_principals = ["account users"], except_principals = ["raw", "partial"], column_mask = { function_name = "cat.sch.gr_mask_email_partial__string__0a1b2c3d_full" } } }] }, { module = "module.data_access", mode = "managed", type = "databricks_entity_tag_assignment", name = "treatment", provider = "provider[\"registry.terraform.io/databricks/databricks\"].workspace", instances = [{ index_key = "columns|cat.sch.orders.email|gr_treatment", schema_version = 0, attributes = { entity_name = "cat.sch.orders.email", tag_value = "email_partial__string__0a1b2c3d" } }] }] })
    }
  }
}

# The live generation, unchanged: nothing to guard.
run "unchanged_generation_plans" {
  command = plan
  variables {
    policy_stage = ""
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
    ]
  }
}

# A treatment change in one apply (new policies, column retagged, old policies dropped).
run "one_shot_retag_is_refused" {
  command         = plan
  expect_failures = [output.staged_policy_guard]
  variables {
    policy_stage = ""
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__9f8e7d6c" },
    ]
  }
}

# Even with the old policies kept, a retag outside switch is refused.
run "one_shot_retag_keeping_old_policies_is_refused" {
  command         = plan
  expect_failures = [output.staged_policy_guard]
  variables {
    policy_stage = ""
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__9f8e7d6c" },
    ]
  }
}

# create: the replacement is added, every live policy and tag stays.
run "create_adds_without_touching_live" {
  command = plan
  variables {
    policy_stage = "create"
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
    ]
  }
}

# create may not drop a live policy.
run "create_may_not_remove_a_live_policy" {
  command         = plan
  expect_failures = [output.staged_policy_guard]
  variables {
    policy_stage = "create"
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
    ]
  }
}

# A tier move in one apply (principals changed in place).
run "one_shot_tier_move_is_refused" {
  command         = plan
  expect_failures = [output.staged_policy_guard]
  variables {
    policy_stage = ""
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["analysts"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "analysts"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
    ]
  }
}

# tighten: TO grows and EXCEPT shrinks, so nobody is unmasked.
run "tighten_may_add_coverage" {
  command = plan
  variables {
    policy_stage = "tighten"
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial", "analysts"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
    ]
  }
}

# tighten may not take a principal out of TO or add one to EXCEPT.
run "tighten_may_not_remove_coverage" {
  command         = plan
  expect_failures = [output.staged_policy_guard]
  variables {
    policy_stage = "tighten"
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["analysts"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial", "analysts"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__0a1b2c3d" },
    ]
  }
}

# switch (only run by the controller after create and verification) retags.
run "switch_retags_under_the_controller" {
  command = plan
  variables {
    policy_stage = "switch"
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__0a1b2c3d_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__0a1b2c3d')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__0a1b2c3d_full", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__9f8e7d6c" },
    ]
  }
}

# retire removes the old generation's policies.
run "retire_removes_under_the_controller" {
  command = plan
  variables {
    policy_stage = "retire"
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__9f8e7d6c" },
    ]
  }
}

# Legacy envs plan exactly as before: the guard is off. (Their grants wait
# for a coverage check, which this test has none of.)
run "legacy_mode_is_never_guarded" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    governance_mode = "legacy"
    policy_stage    = ""
    fgac_policies = [
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_partial", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["partial"], except_principals = ["raw"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_partial", function_catalog = "cat", function_schema = "sch" },
      { name = "gr_mask_cat_email_partial__string__9f8e7d6c_full", policy_type = "POLICY_TYPE_COLUMN_MASK", catalog = "cat", to_principals = ["account users"], except_principals = ["raw", "partial"], match_condition = "hasTagValue('gr_treatment', 'email_partial__string__9f8e7d6c')", match_alias = "masked_col", function_name = "gr_mask_email_partial__string__9f8e7d6c_full", function_catalog = "cat", function_schema = "sch" },
    ]
    tag_assignments = [
      { entity_type = "columns", entity_name = "cat.sch.orders.email", tag_key = "gr_treatment", tag_value = "email_partial__string__9f8e7d6c" },
    ]
  }
}

# Step 8 SELECT (table_readers) with a passing coverage check: every staged
# phase but grant withholds new SELECT, so access opens only after the new
# protection is applied and verified; grant (and a plain plan) opens it.
run "readers_gate_inputs" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
}

run "readers_gate_passes" {
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/staged/data_access/masking_functions.sql" = "SELECT 1;\n"
      "tests/.tmp/staged/data_access/.coverage_gate.json"   = jsonencode({ status = "pass", fingerprint = run.readers_gate_inputs.coverage_gate_inputs.fingerprint, refreshed_at = "@NOW@" })
      "tests/.tmp/staged/data_access/terraform.tfstate"     = jsonencode({ version = 4, outputs = { coverage_gate = { value = { status = "pass", fingerprint = "applied", deployment_binding = run.readers_gate_inputs.coverage_gate_inputs.deployment_binding } } }, resources = [{ module = "module.data_access", mode = "managed", type = "databricks_policy_info", name = "policies", provider = "provider[\"registry.terraform.io/databricks/databricks\"].workspace", instances = [{ index_key = "gr_mask_cat_email_partial__string__0a1b2c3d_partial", schema_version = 0, attributes = { to_principals = ["partial"], except_principals = ["raw"], column_mask = { function_name = "cat.sch.gr_mask_email_partial__string__0a1b2c3d_partial" } } }, { index_key = "gr_mask_cat_email_partial__string__0a1b2c3d_full", schema_version = 0, attributes = { to_principals = ["account users"], except_principals = ["raw", "partial"], column_mask = { function_name = "cat.sch.gr_mask_email_partial__string__0a1b2c3d_full" } } }] }, { module = "module.data_access", mode = "managed", type = "databricks_entity_tag_assignment", name = "treatment", provider = "provider[\"registry.terraform.io/databricks/databricks\"].workspace", instances = [{ index_key = "columns|cat.sch.orders.email|gr_treatment", schema_version = 0, attributes = { entity_name = "cat.sch.orders.email", tag_value = "email_partial__string__0a1b2c3d" } }] }] })
    }
  }
}

# create plans no new SELECT.
run "create_withholds_new_select" {
  command = plan
  variables {
    policy_stage  = "create"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = join(",", module.data_access.withheld_table_grants.grants) == "cat.sch.orders|raw" && module.data_access.withheld_table_grants.status == "pass"
    error_message = "a staged phase before grant must withhold new SELECT"
  }
}

# tighten plans no new SELECT.
run "tighten_withholds_new_select" {
  command = plan
  variables {
    policy_stage  = "tighten"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = join(",", module.data_access.withheld_table_grants.grants) == "cat.sch.orders|raw" && module.data_access.withheld_table_grants.status == "pass"
    error_message = "a staged phase before grant must withhold new SELECT"
  }
}

# switch plans no new SELECT.
run "switch_withholds_new_select" {
  command = plan
  variables {
    policy_stage  = "switch"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = join(",", module.data_access.withheld_table_grants.grants) == "cat.sch.orders|raw" && module.data_access.withheld_table_grants.status == "pass"
    error_message = "a staged phase before grant must withhold new SELECT"
  }
}

# restore plans no new SELECT.
run "restore_withholds_new_select" {
  command = plan
  variables {
    policy_stage  = "restore"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = join(",", module.data_access.withheld_table_grants.grants) == "cat.sch.orders|raw" && module.data_access.withheld_table_grants.status == "pass"
    error_message = "a staged phase before grant must withhold new SELECT"
  }
}

# retire plans no new SELECT.
run "retire_withholds_new_select" {
  command = plan
  variables {
    policy_stage  = "retire"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = join(",", module.data_access.withheld_table_grants.grants) == "cat.sch.orders|raw" && module.data_access.withheld_table_grants.status == "pass"
    error_message = "a staged phase before grant must withhold new SELECT"
  }
}

# abandon plans no new SELECT.
run "abandon_withholds_new_select" {
  command = plan
  variables {
    policy_stage  = "abandon"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = join(",", module.data_access.withheld_table_grants.grants) == "cat.sch.orders|raw" && module.data_access.withheld_table_grants.status == "pass"
    error_message = "a staged phase before grant must withhold new SELECT"
  }
}

# grant, the last phase, plans it.
run "grant_opens_new_select" {
  command = plan
  variables {
    policy_stage  = "grant"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = length(module.data_access.withheld_table_grants.grants) == 0 && module.data_access.withheld_table_grants.status == "pass"
    error_message = "the grant phase opens new SELECT"
  }
}

# Outside the controller (make plan's preview) the gate alone decides, as before.
run "plain_plan_still_grants" {
  command = plan
  variables {
    policy_stage  = "preview"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = length(module.data_access.withheld_table_grants.grants) == 0 && module.data_access.withheld_table_grants.status == "pass"
    error_message = "the grant phase opens new SELECT"
  }
}


# held_access_grants and rebind_table_grants belong to make policy-rebind: in
# any other plan they are ignored (holding a grant back would revoke it), and
# the check says so.
run "rebind_variables_outside_rebind_are_ignored" {
  command         = plan
  expect_failures = [check.rebind_grants_only_in_rebind]
  variables {
    policy_stage        = ""
    held_access_grants  = ["terraform_sp_manage_catalog|cat"]
    rebind_table_grants = ["cat.sch.orders|raw"]
  }
}

run "rebind_variables_in_rebind" {
  command = plan
  variables {
    policy_stage       = "rebind"
    held_access_grants = ["terraform_sp_manage_catalog|cat"]
  }
}

# A staged phase whose coverage check genuinely doesn't pass warns as a plain
# plan does, and points a deterministic env at rehearse (a promoted one at its
# source and a release), never at make apply. Each file below has its own
# state, so it rewrites only the gate result and keeps the applied state.
run "readers_gate_fails" {
  state_key = "gate_fail"
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/staged/data_access/.coverage_gate.json" = jsonencode({ status = "fail", fingerprint = run.readers_gate_inputs.coverage_gate_inputs.fingerprint, refreshed_at = "@NOW@" })
    }
  }
}

run "create_with_failed_gate_warns_rehearse" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage  = "create"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = join(",", module.data_access.withheld_table_grants.grants) == "cat.sch.orders|raw" && module.data_access.withheld_table_grants.status == "failed"
    error_message = "a failed gate withholds new SELECT in a staged phase"
  }
  assert {
    condition     = local.business_select_withheld_next_step == "Run this layer through make (make rehearse ENV=staged), which refreshes live tags and DDL and runs the coverage check."
    error_message = "a deterministic dev env is pointed at make rehearse: ${local.business_select_withheld_next_step}"
  }
  assert {
    condition     = endswith(local.business_select_withheld_message, "Removals and existing grants still apply. Run this layer through make (make rehearse ENV=staged), which refreshes live tags and DDL and runs the coverage check.") && !strcontains(local.business_select_withheld_message, "make apply")
    error_message = "the warning must not advise make apply: ${local.business_select_withheld_message}"
  }
}

run "retire_with_failed_gate_in_promoted_env_warns_release" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage  = "retire"
    promote_from  = "dev"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = module.data_access.withheld_table_grants.status == "failed"
    error_message = "the gate file says fail"
  }
  assert {
    condition     = local.business_select_withheld_next_step == "Fix the cause in dev, make rehearse ENV=dev, make promote-to ENV=staged, then commit and push envs/staged/ to origin/main (or merge its PR), make rehearse ENV=dev on that commit, make release-plan ENV=staged and make release ENV=staged; release-plan refreshes live tags and DDL and runs the coverage check."
    error_message = "a promoted env is pointed at its source and a release: ${local.business_select_withheld_next_step}"
  }
  assert {
    condition     = !strcontains(local.business_select_withheld_message, "make apply")
    error_message = "the warning must not advise make apply: ${local.business_select_withheld_message}"
  }
}

run "readers_gate_stale" {
  state_key = "gate_stale"
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/staged/data_access/.coverage_gate.json" = jsonencode({ status = "pass", fingerprint = "an-older-config", refreshed_at = "@NOW@" })
    }
  }
}

run "switch_with_stale_gate_warns_rehearse" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage  = "switch"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = join(",", module.data_access.withheld_table_grants.grants) == "cat.sch.orders|raw" && module.data_access.withheld_table_grants.status == "stale"
    error_message = "a stale gate withholds new SELECT in a staged phase"
  }
  assert {
    condition     = local.business_select_withheld_next_step == "Run this layer through make (make rehearse ENV=staged), which refreshes live tags and DDL and runs the coverage check."
    error_message = "a deterministic dev env is pointed at make rehearse: ${local.business_select_withheld_next_step}"
  }
}

run "abandon_with_stale_gate_in_promoted_env_warns_release" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage  = "abandon"
    promote_from  = "test"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = module.data_access.withheld_table_grants.status == "stale"
    error_message = "the gate fingerprint is stale"
  }
  assert {
    condition     = local.business_select_withheld_next_step == "Fix the cause in test, make rehearse ENV=test, make promote-to ENV=staged, then commit and push envs/staged/ to origin/main (or merge its PR), make rehearse ENV=test on that commit, make release-plan ENV=staged and make release ENV=staged; release-plan refreshes live tags and DDL and runs the coverage check."
    error_message = "a promoted env is pointed at its own source: ${local.business_select_withheld_next_step}"
  }
}

run "readers_gate_missing" {
  state_key = "gate_missing"
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/staged/data_access/.coverage_gate.json" = null
    }
  }
}

run "tighten_with_missing_gate_warns_rehearse" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    policy_stage  = "tighten"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = module.data_access.withheld_table_grants.status == "missing" && local.business_select_withheld_next_step == "Run this layer through make (make rehearse ENV=staged), which refreshes live tags and DDL and runs the coverage check."
    error_message = "a missing gate points a deterministic dev env at make rehearse: ${local.business_select_withheld_next_step}"
  }
}

# Legacy envs keep the make apply advice, byte for byte.
run "legacy_missing_gate_keeps_apply_advice" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    governance_mode = "legacy"
    policy_stage    = ""
    promote_from    = "dev"
  }
  assert {
    condition     = local.business_select_withheld_message == "Coverage check ${module.data_access.withheld_table_grants.status}: ${module.data_access.withheld_table_grants.problem}. New business SELECT withheld: ${join(", ", module.data_access.withheld_table_grants.grants)}. Removals and existing grants still apply. Run this layer through make (make apply ENV=staged), which refreshes live tags and DDL and runs the coverage check."
    error_message = "legacy advice changed: ${local.business_select_withheld_message}"
  }
}

# prod is promoted even without promote_from (release_gate.env_role).
run "prod_setup" {
  state_key = "prod_files"
  module {
    source = "./tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/prod/data_access/masking_functions.sql" = "SELECT 1;\n"
    }
  }
}

run "prod_missing_gate_warns_release" {
  command         = plan
  expect_failures = [check.business_select_withheld]
  variables {
    env_dir       = "tests/.tmp/prod/data_access"
    policy_stage  = "create"
    table_readers = { "cat.sch.orders" = ["raw"] }
  }
  assert {
    condition     = module.data_access.withheld_table_grants.status == "missing" && local.business_select_withheld_next_step == "Fix the cause in dev, make rehearse ENV=dev, make promote-to ENV=prod, then commit and push envs/prod/ to origin/main (or merge its PR), make rehearse ENV=dev on that commit, make release-plan ENV=prod and make release ENV=prod; release-plan refreshes live tags and DDL and runs the coverage check."
    error_message = "prod is pointed at dev and a release: ${local.business_select_withheld_next_step}"
  }
}
