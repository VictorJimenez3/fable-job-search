import {z} from "zod";
import {
  ApplicationSchema,
  CompanySchema,
  PostingPageSchema,
  type Application,
  type Company,
  type JobFilters,
  type PostingPage,
} from "./contracts";

async function jsonResponse(response: Response): Promise<unknown> {
  const body: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      typeof body === "object" && body && "error" in body
        ? String(body.error)
        : `API returned ${response.status}`,
    );
  }
  return body;
}

export async function loadJobs(filters: JobFilters, cursor = ""): Promise<PostingPage> {
  const query = new URLSearchParams({
    profile: filters.profile,
    q: filters.query,
    freshness: filters.freshness,
    eligibility: filters.eligibility,
    limit: "50",
  });
  if (cursor) query.set("cursor", cursor);
  const response = await fetch(`/api/v1/jobs?${query}`, {headers: {Accept: "application/json"}});
  return PostingPageSchema.parse(await jsonResponse(response));
}

export async function saveApplication(
  postingId: string,
  profile: JobFilters["profile"],
  stage = "saved",
): Promise<Application> {
  const response = await fetch("/api/v1/applications", {
    method: "POST",
    headers: {Accept: "application/json", "Content-Type": "application/json"},
    body: JSON.stringify({posting_id: postingId, profile, stage}),
  });
  return z.object({data: ApplicationSchema}).parse(await jsonResponse(response)).data;
}

export async function loadApplications(profile: JobFilters["profile"]): Promise<Application[]> {
  const response = await fetch(`/api/v1/applications?profile=${encodeURIComponent(profile)}`, {
    headers: {Accept: "application/json"},
  });
  return z.object({data: z.array(ApplicationSchema)}).parse(await jsonResponse(response)).data;
}

export async function loadCompanies(profile: JobFilters["profile"]): Promise<Company[]> {
  const response = await fetch(`/api/v1/companies?profile=${encodeURIComponent(profile)}`, {
    headers: {Accept: "application/json"},
  });
  return z.object({data: z.array(CompanySchema)}).parse(await jsonResponse(response)).data;
}
