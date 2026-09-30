/**
 * A document's display name, shared by the Workspace header and the Quiz tab.
 *
 * The server sends a `title` read from the document's own first lines
 * (quiz_service.document_title). This is the fallback for anything that
 * still looks like a filename: extension off, underscores to spaces.
 */
export function displayName(name) {
  const text = String(name || "");
  if (!/\.[a-z0-9]{2,5}$/i.test(text) && !text.includes("_")) return text;
  return text.replace(/\.[a-z0-9]{2,5}$/i, "").replace(/[\s_]+/g, " ").trim() || text;
}

/**
 * The best name for a document row: its title, else its cleaned filename.
 * The title is shown as sent. Run through displayName, "Lecture 14.01" lost
 * its ".01" as if it were a file extension.
 */
export const documentTitle = (doc) => doc?.title || displayName(doc?.filename || doc?.name);
