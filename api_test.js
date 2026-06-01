// API test runner for the Personal Execution OS REST API.
// Run with: node api_test.js
// Requires Node 18+ (built-in fetch). No dependencies needed.
//
// Tests run sequentially so later tests can reuse IDs created by earlier ones.
// Each test prints: label | status code | response body

const BASE = "http://localhost:5000";

// Paste your actual API key here before running.
const API_KEY = "R!<%R19%)v!4*Es4Dn_7`@CP|r[.%X|xQI(Po@I{zC)7'-ZR";

// ─── helper ───────────────────────────────────────────────────────────────────

// expectedStatus lets you declare what HTTP status code a test should return.
// If omitted, the test passes on any 2xx. Use it for tests that expect errors
// (400, 404, etc.) so they show ✓ instead of ✗.
// Pass apiKey=null to deliberately omit the Authorization header.
async function hit(label, method, path, body = null, expectedStatus = null, apiKey = API_KEY) {
  const headers = { "Content-Type": "application/json" };
  if (apiKey) headers["Authorization"] = `Bearer ${apiKey}`;

  const options = { method, headers };
  if (body) options.body = JSON.stringify(body);

  const res = await fetch(`${BASE}${path}`, options);
  const data = await res.json().catch(() => null);  // graceful if body isn't JSON

  const passed = expectedStatus ? res.status === expectedStatus : res.ok;
  const marker = passed ? "✓" : "✗";
  console.log(`\n${marker} [${res.status}] ${label}`);
  console.log(JSON.stringify(data, null, 2));

  return { status: res.status, data };
}

// ─── tests ────────────────────────────────────────────────────────────────────

async function main() {
  console.log("=== API Test Suite ===");
  console.log(`Target: ${BASE}\n`);

  // ── Auth ──────────────────────────────────────────────────────────────────

  // No Authorization header at all — should be rejected
  await hit("No auth header (expect 401)", "GET", "/api/health", null, 401, null);

  // Wrong key — should be rejected
  await hit("Wrong API key (expect 401)", "GET", "/api/health", null, 401, "totally-wrong-key");

  // Valid key — should pass through
  await hit("Valid API key (expect 200)", "GET", "/api/health", null, 200);


  // ── Health ────────────────────────────────────────────────────────────────
  await hit("Health check", "GET", "/api/health");


  // ── Projects ──────────────────────────────────────────────────────────────

  // Create a project — no id in body means INSERT
  const { data: proj } = await hit("Create project", "POST", "/api/projects", {
    title: "Test Project",
    description: "Created by api_test.js",
    status: "active",
  });
  const projId = proj?.id;

  // Get the project we just created
  await hit("Get project by id", "GET", `/api/projects/${projId}`);

  // List all projects
  await hit("List all projects", "GET", "/api/projects");

  // List with status filter
  await hit("List projects filtered by status=active", "GET", "/api/projects?status=active");

  // Partial update — only send fields you want changed, rest stays untouched
  await hit("Update project (partial — progress only)", "POST", "/api/projects", {
    id: projId,
    progress: 42,
  });

  // Try updating with no valid fields — should get a 400
  await hit("Update project with no valid fields (expect 400)", "POST", "/api/projects", {
    id: projId,
    garbage_field: "ignored",
  }, 400);

  // Get a project that doesn't exist — should get a 404
  await hit("Get non-existent project (expect 404)", "GET", "/api/projects/999999", null, 404);


  // ── Tasks ─────────────────────────────────────────────────────────────────

  // Create a task linked to the project above
  const { data: task } = await hit("Create task", "POST", "/api/tasks", {
    title: "Write the recommendation engine",
    description: "Score tasks by urgency + actionability + momentum - resistance",
    status: "inbox",
    priority: "high",
    energy_type: "deep_focus",
    fear_level: 7,
    ambiguity_level: 5,
    estimated_effort: 120,
    project_id: projId,
  });
  const taskId = task?.id;

  // Create a second task (no project) to have something to filter against
  await hit("Create second task (low priority, no project)", "POST", "/api/tasks", {
    title: "Reply to emails",
    priority: "low",
    energy_type: "light_admin",
    fear_level: 1,
  });

  // Get the first task by id
  await hit("Get task by id", "GET", `/api/tasks/${taskId}`);

  // List all tasks
  await hit("List all tasks", "GET", "/api/tasks");

  // Filter by status
  await hit("List tasks filtered by status=inbox", "GET", "/api/tasks?status=inbox");

  // Filter by priority
  await hit("List tasks filtered by priority=high", "GET", "/api/tasks?priority=high");

  // Filter by project_id
  await hit(`List tasks filtered by project_id=${projId}`, "GET", `/api/tasks?project_id=${projId}`);

  // Partial update — mark as active, bump priority to critical
  await hit("Update task (status + priority)", "POST", "/api/tasks", {
    id: taskId,
    status: "active",
    priority: "critical",
  });

  // Partial update — only fear_level
  await hit("Update task (fear_level only)", "POST", "/api/tasks", {
    id: taskId,
    fear_level: 3,
  });

  // Send unknown fields — should be silently ignored, not error
  await hit("Update task with mix of valid + junk fields (junk ignored)", "POST", "/api/tasks", {
    id: taskId,
    title: "Write the recommendation engine (renamed)",
    not_a_real_field: "should be ignored",
  });

  // Create without a title — should get a 400
  await hit("Create task with no title (expect 400)", "POST", "/api/tasks", {
    priority: "high",
  }, 400);

  // Update with no valid fields — should get a 400
  await hit("Update task with no valid fields (expect 400)", "POST", "/api/tasks", {
    id: taskId,
    junk: "nothing here",
  }, 400);

  // Get a task that doesn't exist — should get a 404
  await hit("Get non-existent task (expect 404)", "GET", "/api/tasks/999999", null, 404);

  // Delete the task
  await hit("Delete task", "DELETE", `/api/tasks/${taskId}`);

  // Confirm it's gone — should get a 404
  await hit("Get deleted task (expect 404)", "GET", `/api/tasks/${taskId}`, null, 404);

  // Delete non-existent task — should get a 404
  await hit("Delete non-existent task (expect 404)", "DELETE", "/api/tasks/999999", null, 404);


  // ── Cleanup ───────────────────────────────────────────────────────────────

  // Delete the test project
  await hit("Delete project", "DELETE", `/api/projects/${projId}`);

  // Confirm it's gone
  await hit("Get deleted project (expect 404)", "GET", `/api/projects/${projId}`, null, 404);


  console.log("\n=== Done ===");
}

main().catch(console.error);
