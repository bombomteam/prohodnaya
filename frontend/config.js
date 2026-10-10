// Set mode to "api" and point apiBaseUrl at the team server for integration.
// Demo mode contains sample data and simulated outcomes; it is never security proof.
export const config = {
  mode: "demo",
  apiBaseUrl: "http://localhost:8000/api",
  pollingMs: 2000,
  locationIntervalMs: 15000,
};
