import {z} from "zod";

export const PostingSchema = z.object({
  public_id: z.string(),
  legacy_id: z.string().optional().default(""),
  profile: z.enum(["new_grad", "internship"]).default("new_grad"),
  company: z.string(),
  title: z.string(),
  url: z.string(),
  locations: z.array(z.string()).default([]),
  remote: z.boolean().default(false),
  posted_at: z.number().nullable().optional(),
  last_seen_at: z.number().nullable().optional(),
  salary: z.string().default(""),
  sector: z.string().default(""),
  evidence_score: z.number().min(0).max(100),
  eligibility: z.enum(["eligible", "review", "excluded"]),
  priority_tier: z.enum(["goal", "recommended", "explore"]),
  score_reasons: z.array(z.string()).default([]),
  status: z.enum(["open", "expired", "filled", "archived"]).default("open"),
});

export const PostingPageSchema = z.object({
  data: z.array(PostingSchema),
  next_cursor: z.string().nullable(),
  generated_at: z.string(),
  source: z.enum(["postgres", "legacy-fallback"]),
  total: z.number().optional(),
});

export type Posting = z.infer<typeof PostingSchema>;
export type PostingPage = z.infer<typeof PostingPageSchema>;

export const ApplicationSchema = z.object({
  id: z.string(),
  posting_id: z.string().nullable().optional(),
  stage: z.string(),
  company: z.string(),
  title: z.string(),
  url: z.string().nullable().optional().default(""),
  updated_at: z.string(),
});

export const CompanySchema = z.object({
  company: z.string(),
  website: z.string().default(""),
  open_postings: z.number().default(0),
  last_seen_at: z.string().nullable().optional(),
  metadata: z.record(z.string(), z.unknown()).default({}),
});

export type Application = z.infer<typeof ApplicationSchema>;
export type Company = z.infer<typeof CompanySchema>;

export type JobFilters = {
  profile: "new_grad" | "internship";
  query: string;
  freshness: "action" | "7d" | "30d" | "all";
  eligibility: "eligible" | "review" | "all";
};

export function safeHttpURL(value: string): string {
  try {
    const parsed = new URL(value);
    if (!["http:", "https:"].includes(parsed.protocol) || parsed.username || parsed.password) return "";
    return parsed.href;
  } catch {
    return "";
  }
}
