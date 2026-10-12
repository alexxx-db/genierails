# A captured agent (agents/<name>.space.json, captured = true from the
# agent-file loader) gets no config from Terraform: dev's stays as curated in
# the UI, and release pushes prod's from the captured file.
mock_provider "databricks" {
  alias = "account"
}
mock_provider "databricks" {
  alias = "workspace"
}
mock_provider "null" {}

variables {
  env_dir                   = "tests/.tmp/captured-agent"
  databricks_account_id     = "account"
  databricks_client_id      = "service-principal"
  databricks_client_secret  = "secret"
  databricks_workspace_id   = "123"
  databricks_workspace_host = "https://example.invalid"
  genie_only                = true
  groups                    = {}
  genie_space_configs = {
    Captured = { description = "a generated draft that must not reach the agent" }
    Drafted  = { description = "a generated draft that does reach the agent" }
  }
}

run "captured_agent_gets_no_config_update" {
  command = plan

  variables {
    genie_spaces = [
      {
        name           = "Captured"
        genie_space_id = "space-1"
        uc_tables      = ["cat.schema.table"]
        captured       = true
      },
      {
        name           = "Drafted"
        genie_space_id = "space-2"
        uc_tables      = ["cat.schema.table"]
      },
    ]
  }

  assert {
    condition     = !contains(keys(output.genie_existing_space_warehouse_intent), "captured")
    error_message = "a captured agent must not get update-config from Terraform"
  }

  assert {
    condition     = contains(keys(output.genie_existing_space_warehouse_intent), "drafted")
    error_message = "an agent without a capture keeps its generated config push"
  }
}
