// Only the three object areas named in the supplied case package.
export const CASE_ZONES = Object.freeze(["Бетономешалка", "Котлован", "Курилка"]);

export function caseZoneName(value) {
  const name = String(value || "").trim().toLocaleLowerCase("ru-RU");
  if (name === "бетономешалка") return CASE_ZONES[0];
  if (name === "котлован") return CASE_ZONES[1];
  if (name === "курилка" || name === "место для курения") return CASE_ZONES[2];
  return null;
}

export function caseZoneById(id, zones) {
  if (id == null) return null;
  return caseZoneName(zones.find(zone => String(zone.id) === String(id))?.name);
}

export function caseZoneMetrics(metrics) {
  return CASE_ZONES.map(name => {
    const match = metrics.find(zone => caseZoneName(zone.name) === name);
    return { name, available: Boolean(match), count: Number(match?.count || 0), level: match?.level || null, id: match?.id ?? null };
  });
}
