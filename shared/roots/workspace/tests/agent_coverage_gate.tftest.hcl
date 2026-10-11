# Rollout step 8, deterministic envs: CAN_RUN comes only from each agent's
# acl_groups and opens only on a current pass of the live agent coverage
# check (scripts/agent_coverage.py writes .agent_coverage.json), never from
# the data_access layer's state:
#   - a missing, forged (wrong fingerprint), stale (inputs changed) or expired
#     result, or an agent the check failed, withholds every new group;
#   - a current pass for the agent opens it, with no data_access state at all;
#   - drafted or derived ACLs (genie_space_configs) never grant CAN_RUN, and no
#     groups map is needed;
#   - removing groups always applies, whatever the check says.

mock_provider "databricks" {
  alias = "account"
}
mock_provider "databricks" {
  alias = "workspace"
}
mock_provider "null" {}

override_data {
  target = module.workspace.data.databricks_group.existing
  values = {
    id = 123
  }
}

variables {
  env_dir                   = "tests/.tmp/agentgate"
  databricks_account_id     = "account"
  databricks_client_id      = "service-principal"
  databricks_client_secret  = "secret"
  databricks_workspace_id   = "123"
  databricks_workspace_host = "https://example.invalid"
  sql_warehouse_id          = "warehouse"
  governance_mode           = "deterministic"
  groups                    = {}
  genie_spaces              = [{ name = "Sales", genie_space_id = "space-1", uc_tables = ["cat.sch.orders"], acl_groups = ["partial"] }]
  # A drafted/derived ACL that must never become CAN_RUN.
  genie_space_configs = { Sales = { acl_groups = ["drafted"] } }
}

run "clean" {
  module {
    source = "../data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/agentgate/.agent_coverage.json"          = null
      "tests/.tmp/agentgate/terraform.tfstate"             = null
      "tests/.tmp/agentgate/data_access/terraform.tfstate" = null
    }
  }
}

run "missing_result_withholds" {
  command = plan
  expect_failures = [check.genie_can_run_withheld]
  assert {
    condition     = strcontains(output.genie_exposure_blocker, "live agent coverage check") && strcontains(output.genie_exposure_blocker, "no coverage check result exists")
    error_message = "a missing live check result must block new CAN_RUN"
  }
  assert {
    condition     = join(",", lookup(output.genie_space_can_run_withheld, "sales", [])) == "partial"
    error_message = "the agent's acl_groups must be withheld"
  }
  assert {
    condition     = output.genie_space_acls_groups["sales"] == "partial"
    error_message = "CAN_RUN must come only from genie_spaces[].acl_groups (not the drafted config), with no groups map"
  }
}

run "write_pass" {
  module {
    source = "../data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/agentgate/.agent_coverage.json" = jsonencode({ status = "pass", fingerprint = run.missing_result_withholds.agent_coverage_inputs.fingerprint, refreshed_at = "@NOW@", agents = { sales = { status = "pass", problems = [] } } })
    }
  }
}

run "current_pass_opens_can_run_without_data_access_state" {
  command = plan
  assert {
    condition     = output.genie_exposure_blocker == "" && length(lookup(output.genie_space_can_run_withheld, "sales", [])) == 0
    error_message = "a current pass must open the agent's acl_groups"
  }
  assert {
    condition     = module.workspace.genie_space_acl_groups_planned["sales"] == "partial"
    error_message = "the ACL must grant exactly acl_groups"
  }
}

run "forged_fingerprint" {
  module {
    source = "../data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/agentgate/.agent_coverage.json" = jsonencode({ status = "pass", fingerprint = "forged", refreshed_at = "@NOW@", agents = { sales = { status = "pass", problems = [] } } })
    }
  }
}

run "forged_fingerprint_withholds" {
  command = plan
  expect_failures = [check.genie_can_run_withheld]
  assert {
    condition     = strcontains(output.genie_exposure_blocker, "inputs changed") && join(",", lookup(output.genie_space_can_run_withheld, "sales", [])) == "partial"
    error_message = "a result for other inputs (or a hand-edited fingerprint) must withhold CAN_RUN"
  }
}

run "expired_result" {
  module {
    source = "../data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/agentgate/.agent_coverage.json" = jsonencode({ status = "pass", fingerprint = run.missing_result_withholds.agent_coverage_inputs.fingerprint, refreshed_at = "2000-01-01T00:00:00Z", agents = { sales = { status = "pass", problems = [] } } })
    }
  }
}

run "expired_result_withholds" {
  command = plan
  expect_failures = [check.genie_can_run_withheld]
  assert {
    condition     = strcontains(output.genie_exposure_blocker, "older than") && join(",", lookup(output.genie_space_can_run_withheld, "sales", [])) == "partial"
    error_message = "a result older than coverage_gate_max_age must withhold CAN_RUN"
  }
}

run "stale_after_widening" {
  module {
    source = "../data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/agentgate/.agent_coverage.json" = jsonencode({ status = "pass", fingerprint = run.missing_result_withholds.agent_coverage_inputs.fingerprint, refreshed_at = "@NOW@", agents = { sales = { status = "pass", problems = [] } } })
    }
  }
}

# The pass was for acl_groups = ["partial"]; adding a group makes it stale.
run "added_group_needs_a_new_check" {
  command = plan
  expect_failures = [check.genie_can_run_withheld]
  variables {
    genie_spaces = [{ name = "Sales", genie_space_id = "space-1", uc_tables = ["cat.sch.orders"], acl_groups = ["partial", "full"] }]
  }
  assert {
    condition     = join(",", lookup(output.genie_space_can_run_withheld, "sales", [])) == "full,partial"
    error_message = "changing an agent's groups must invalidate the check"
  }
}

run "agent_failed" {
  module {
    source = "../data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/agentgate/.agent_coverage.json" = jsonencode({ status = "pass", fingerprint = run.missing_result_withholds.agent_coverage_inputs.fingerprint, refreshed_at = "@NOW@", agents = { sales = { status = "fail", problems = ["cat.sch.orders.email is classified (class.email_address) but no column mask applies to it"] } } })
    }
  }
}

run "failed_agent_withholds" {
  command = plan
  expect_failures = [check.genie_can_run_withheld]
  assert {
    condition     = output.genie_exposure_blocker == "" && strcontains(join(" ", output.genie_space_missing_grants["sales"]), "no column mask") && join(",", lookup(output.genie_space_can_run_withheld, "sales", [])) == "partial"
    error_message = "an agent the live check failed must get no new CAN_RUN"
  }
}

# Applied: partial and full. Desired: partial only. With no check result at
# all the removal still applies (and partial is kept).
run "applied_two_groups" {
  module {
    source = "../data_access/tests/file_writer"
  }
  variables {
    files = {
      "tests/.tmp/agentgate/.agent_coverage.json" = null
      "tests/.tmp/agentgate/terraform.tfstate"    = jsonencode({ version = 4, outputs = {}, resources = [{ module = "module.workspace", mode = "managed", type = "null_resource", name = "genie_space_acls", instances = [{ index_key = "sales", attributes = { id = "1", triggers = { space_id = "space-1", groups = "full,partial" } } }] }] })
    }
  }
}

run "removal_applies_without_a_check" {
  command = plan
  assert {
    condition     = length(lookup(output.genie_space_can_run_withheld, "sales", [])) == 0 && module.workspace.genie_space_acl_groups_planned["sales"] == "partial"
    error_message = "shrinking CAN_RUN must apply whatever the live check says"
  }
}

run "clearing_applies_without_a_check" {
  command = plan
  variables {
    genie_spaces = [{ name = "Sales", genie_space_id = "space-1", uc_tables = ["cat.sch.orders"], acl_groups = [] }]
  }
  assert {
    condition     = length(lookup(output.genie_space_can_run_withheld, "sales", [])) == 0 && module.workspace.genie_space_acl_groups_planned["sales"] == ""
    error_message = "acl_groups = [] must clear CAN_RUN whatever the live check says"
  }
}

# Legacy envs keep the data_access-state gate exactly as before.
run "legacy_still_reads_data_access_state" {
  command = plan
  expect_failures = [check.genie_can_run_withheld]
  variables {
    governance_mode = "legacy"
    groups          = { partial = {} }
  }
  assert {
    condition     = strcontains(output.genie_exposure_blocker, "data_access layer has no readable state")
    error_message = "legacy CAN_RUN must still rest on the data_access state"
  }
}
