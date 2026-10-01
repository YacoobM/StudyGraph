/**
 * Display names for documents (Workspace header, Quiz tab). The server sends a title read from
 * the document; displayName is the fallback that cleans a filename.
 */
export function displayName(name) {
  const text = String(name || "");
  if (!/\.[a-z0-9]{2,5}$/i.test(text) && !text.includes("_")) return text;
  return text.replace(/\.[a-z0-9]{2,5}$/i, "").replace(/[\s_]+/g, " ").trim() || text;
}

/**
 * A document row's name: its title as sent, else its cleaned filename. The title skips
 * displayName, which cut "Lecture 14.01" to "Lecture 14".
 */
export const documentTitle = (doc) => doc?.title || displayName(doc?.filename || doc?.name);
