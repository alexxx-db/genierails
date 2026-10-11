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

