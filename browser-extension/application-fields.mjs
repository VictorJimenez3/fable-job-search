function fileFieldText(field) {
  return [
    field?.label, field?.question, field?.group_question, field?.name,
    field?.id, field?.autocomplete, field?.placeholder,
  ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim().toLowerCase();
}

function isFileField(field) {
  return String(field?.type || "").toLowerCase() === "file";
}

function isNonResumeAttachment(field) {
  return /\b(cover\s*letter|transcript|portfolio|work\s*sample|writing\s*sample|supporting|attachment|certificate|photo|headshot)\b/i.test(fileFieldText(field));
}

function isNamedResumeField(field) {
  return !isNonResumeAttachment(field) && /\b(resume|curriculum\s+vitae|cv)\b/i.test(fileFieldText(field));
}

function eligibleResumeFields(fields) {
  return (Array.isArray(fields) ? fields : []).filter(field => isFileField(field) && !isNonResumeAttachment(field));
}

function resumeFileAccepted(fields) {
  const eligible = eligibleResumeFields(fields);
  if (eligible.some(field => String(field?.value || "").trim() && (isNamedResumeField(field) || Boolean(field?.required)))) return true;
  return eligible.length === 1 && Boolean(String(eligible[0]?.value || "").trim());
}

function resumeFieldsNeedingUpload(fields) {
  const allFiles = (Array.isArray(fields) ? fields : []).filter(isFileField);
  const eligible = eligibleResumeFields(fields);
  if (!eligible.length || resumeFileAccepted(fields)) return [];
  const empty = eligible.filter(field => !String(field?.value || "").trim());
  const named = empty.filter(isNamedResumeField);
  const namedRequired = named.find(field => Boolean(field?.required));
  if (namedRequired) return [namedRequired];
  if (named.length) return [named[0]];
  const required = empty.find(field => Boolean(field?.required));
  if (required) return [required];
  // An unnamed single file input is a common ATS resume control. Once a
  // second attachment exists, ambiguity is unsafe and the agent pauses.
  if (allFiles.length === 1 && empty.length === 1) return [empty[0]];
  return [];
}

export {
  fileFieldText,
  isNonResumeAttachment,
  isNamedResumeField,
  resumeFileAccepted,
  resumeFieldsNeedingUpload,
};
