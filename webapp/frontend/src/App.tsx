import {memo, useCallback, useEffect, useMemo, useRef, useState} from "react";
import {useInfiniteQuery, useMutation, useQuery, useQueryClient} from "@tanstack/react-query";
import {useVirtualizer} from "@tanstack/react-virtual";
import {HashRouter, Navigate, NavLink, Route, Routes} from "react-router-dom";
import {loadApplications, loadCompanies, loadJobs, saveApplication} from "./api";
import {safeHttpURL, type JobFilters, type Posting} from "./contracts";
import "./styles.css";

const initialFilters: JobFilters = {
  profile: "new_grad",
  query: "",
  freshness: "action",
  eligibility: "eligible",
};

function ageLabel(epoch?: number | null): string {
  if (!epoch) return "date unknown · verified recently";
  const days = Math.max(0, Math.floor((Date.now() / 1000 - epoch) / 86400));
  if (days === 0) return "today";
  if (days === 1) return "yesterday";
  return `${days} days ago`;
}

function applicationStageLabel(stage: string): string {
  return ({
    saved: "To apply",
    to_tailor: "To tailor",
    applied: "Applied",
    oa: "Online assessment",
    interview: "Interview",
    rejected: "Rejected",
    closed: "Closed",
  } as Record<string, string>)[stage] ?? stage.replaceAll("_", " ");
}

const JobCard = memo(function JobCard({
  job,
  selected,
  saving,
  onSelect,
  onSave,
}: {
  job: Posting;
  selected: boolean;
  saving: boolean;
  onSelect: (id: string) => void;
  onSave: (job: Posting) => void;
}) {
  const href = safeHttpURL(job.url);
  return (
    <article className={`job-card${selected ? " selected" : ""}`} aria-labelledby={`title-${job.public_id}`}>
      <button className="job-main" type="button" onClick={() => onSelect(job.public_id)} aria-expanded={selected}>
        <span className="score" aria-label={`Evidence score ${job.evidence_score} out of 100`}>{job.evidence_score}</span>
        <span className="job-copy">
          <span className="eyebrow">{job.company} · {ageLabel(job.posted_at)}</span>
          <strong id={`title-${job.public_id}`}>{job.title}</strong>
          <span className="meta">{job.locations.join(" · ") || "Location not listed"}{job.salary ? ` · ${job.salary}` : ""}</span>
          <span className="badges">
            {job.priority_tier === "goal" && <span className="badge goal">goal company</span>}
            <span className={`badge ${job.eligibility}`}>{job.eligibility}</span>
            {job.remote && <span className="badge">remote</span>}
          </span>
        </span>
      </button>
      <div className="job-actions">
        {href ? <a className="primary" href={href} target="_blank" rel="noopener noreferrer">Open posting</a> : <span>Unsafe posting URL blocked</span>}
        <button type="button" disabled={saving} onClick={() => onSave(job)}>{saving ? "Saving…" : "Save to pipeline"}</button>
        <a href={`/?job=${encodeURIComponent(job.legacy_id || job.public_id)}`}>Classic details</a>
      </div>
      {selected && (
        <div className="explanation">
          <h3>Why this is in the queue</h3>
          <p>Evidence score measures the posting. Eligibility and your goal priority remain separate.</p>
          <ul>{job.score_reasons.slice(0, 12).map((reason) => <li key={reason}>{reason}</li>)}</ul>
        </div>
      )}
    </article>
  );
});

function LaneSwitch({profile, onChange}: {profile: JobFilters["profile"]; onChange: (profile: JobFilters["profile"]) => void}) {
  return (
    <div className="lane-switch" aria-label="Job lane">
      <button className={profile === "new_grad" ? "active" : ""} onClick={() => onChange("new_grad")}>New grad</button>
      <button className={profile === "internship" ? "active" : ""} onClick={() => onChange("internship")}>Internships</button>
    </div>
  );
}

function JobsView({profile, setProfile}: {profile: JobFilters["profile"]; setProfile: (profile: JobFilters["profile"]) => void}) {
  const [filters, setFilters] = useState({...initialFilters, profile});
  const [draftQuery, setDraftQuery] = useState("");
  const [selected, setSelected] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [actionMessage, setActionMessage] = useState("");
  const parentRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();

  useEffect(() => setFilters((current) => ({...current, profile})), [profile]);
  useEffect(() => {
    const timer = window.setTimeout(() => setFilters((current) => ({...current, query: draftQuery.trim()})), 250);
    return () => window.clearTimeout(timer);
  }, [draftQuery]);

  const jobsQuery = useInfiniteQuery({
    queryKey: ["jobs", filters],
    queryFn: ({pageParam}) => loadJobs(filters, pageParam),
    initialPageParam: "",
    getNextPageParam: (page) => page.next_cursor || undefined,
  });
  const jobs = useMemo(() => jobsQuery.data?.pages.flatMap((page) => page.data) ?? [], [jobsQuery.data]);
  const virtualizer = useVirtualizer({
    count: jobs.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 205,
    overscan: 5,
  });
  const virtualItems = virtualizer.getVirtualItems();
  const lastVirtualIndex = virtualItems.at(-1)?.index ?? -1;
  const {fetchNextPage, hasNextPage, isFetchingNextPage} = jobsQuery;

  useEffect(() => {
    if (lastVirtualIndex >= jobs.length - 8 && hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [fetchNextPage, hasNextPage, isFetchingNextPage, jobs.length, lastVirtualIndex]);

  const saveMutation = useMutation({
    mutationFn: (job: Posting) => saveApplication(job.public_id, filters.profile),
    onSuccess: (application) => {
      setActionMessage(`${application.company} saved to your pipeline.`);
      void queryClient.invalidateQueries({queryKey: ["applications", filters.profile]});
    },
    onError: (error) => setActionMessage(`${error.message}. The classic tracker is still available.`),
  });
  const update = <K extends keyof JobFilters>(key: K, value: JobFilters[K]) => {
    setFilters((current) => ({...current, [key]: value}));
    setSelected("");
  };
  const changeProfile = (next: JobFilters["profile"]) => {
    setProfile(next);
    update("profile", next);
  };
  const toggleSelected = useCallback((id: string) => setSelected((value) => value === id ? "" : id), []);

  return (
    <>
      <section className="hero" aria-labelledby="queue-title">
        <div><p className="kicker">Victor’s action queue</p><h2 id="queue-title">Roles worth opening now</h2><p>Eligibility, source health, freshness, and evidence score stay separate and explainable.</p></div>
        <LaneSwitch profile={profile} onChange={changeProfile} />
      </section>
      <section className="controls" aria-label="Job filters">
        <label className="search"><span>Search</span><input value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} placeholder="Company, title, location" /></label>
        <button className="filter-toggle" type="button" onClick={() => setFiltersOpen((value) => !value)} aria-expanded={filtersOpen}>Filters</button>
        <div className={`filter-fields${filtersOpen ? " open" : ""}`}>
          <label><span>Freshness</span><select value={filters.freshness} onChange={(event) => update("freshness", event.target.value as JobFilters["freshness"])}><option value="action">Action queue</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option><option value="all">All time</option></select></label>
          <label><span>Eligibility</span><select value={filters.eligibility} onChange={(event) => update("eligibility", event.target.value as JobFilters["eligibility"])}><option value="eligible">Eligible</option><option value="review">Needs review</option><option value="all">All</option></select></label>
        </div>
      </section>
      <div className="status" role="status" aria-live="polite">
        {actionMessage || (jobsQuery.isLoading ? "Loading the action queue…" : jobsQuery.isError ? `Could not load jobs: ${jobsQuery.error.message}` : `${jobs.length.toLocaleString()} loaded${jobsQuery.hasNextPage ? " · more available" : ""}`)}
      </div>
      <div className="job-list" ref={parentRef} tabIndex={-1}>
        <div style={{height: virtualizer.getTotalSize(), position: "relative"}}>
          {virtualItems.map((row) => {
            const job = jobs[row.index];
            return <div key={job.public_id} ref={virtualizer.measureElement} data-index={row.index} style={{position: "absolute", top: 0, left: 0, width: "100%", transform: `translateY(${row.start}px)`, paddingBottom: 12}}><JobCard job={job} selected={selected === job.public_id} saving={saveMutation.isPending && saveMutation.variables?.public_id === job.public_id} onSelect={toggleSelected} onSave={(item) => saveMutation.mutate(item)} /></div>;
          })}
        </div>
      </div>
    </>
  );
}

function ApplicationsView({profile}: {profile: JobFilters["profile"]}) {
  const query = useQuery({queryKey: ["applications", profile], queryFn: () => loadApplications(profile), retry: false});
  return (
    <section className="workspace"><p className="kicker">Application timeline</p><h2>Your pipeline</h2>
      {query.isLoading && <p>Loading applications…</p>}
      {query.isError && <div className="empty-state"><p>{query.error.message}</p><a href="/#pipeline">Open the classic pipeline</a></div>}
      <div className="table-list">{query.data?.map((item) => <article key={item.id}><span className="badge">{applicationStageLabel(item.stage)}</span><div><strong>{item.company}</strong><p>{item.title}</p></div><time>{new Date(item.updated_at).toLocaleDateString()}</time></article>)}</div>
    </section>
  );
}

function CompaniesView({profile}: {profile: JobFilters["profile"]}) {
  const query = useQuery({queryKey: ["companies", profile], queryFn: () => loadCompanies(profile)});
  return (
    <section className="workspace"><p className="kicker">Source-grounded research</p><h2>Companies</h2>
      {query.isLoading && <p>Loading companies…</p>}
      {query.isError && <p role="alert">{query.error.message}</p>}
      {!query.isLoading && !query.data?.length && <div className="empty-state"><p>Company cards remain in the classic UI until Postgres cutover.</p><a href="/#companies">Open company research</a></div>}
      <div className="company-grid">{query.data?.map((item) => <article key={item.company}><span>{item.open_postings} open roles</span><h3>{item.company}</h3>{safeHttpURL(item.website) && <a href={item.website} target="_blank" rel="noopener noreferrer">Company site</a>}</article>)}</div>
    </section>
  );
}

function LegacyView({kind}: {kind: "resume" | "settings"}) {
  const copy = kind === "resume"
    ? ["Resume Studio", "The private Mac engine, evidence graph, artifact bank, and review gates remain available in the proven workspace.", "/#testing", "Open Resume Studio"]
    : ["Settings", "Authentication, tracker connections, score controls, and notification preferences remain available during the staged database migration.", "/#settings", "Open settings"];
  return <section className="workspace"><p className="kicker">No workflow lost</p><h2>{copy[0]}</h2><div className="empty-state"><p>{copy[1]}</p><a href={copy[2]}>{copy[3]}</a></div></section>;
}

function Shell() {
  const [profile, setProfile] = useState<JobFilters["profile"]>("new_grad");
  return (
    <div className="app-shell">
      <header><div><span className="brand-mark">JR</span><div><h1>Job Radar</h1><p>Fresh, eligible opportunities first.</p></div></div>
        <nav aria-label="Primary"><NavLink to="/jobs">Jobs</NavLink><NavLink to="/applications">Applications</NavLink><NavLink to="/companies">Companies</NavLink><NavLink to="/resume">Resume Studio</NavLink><NavLink to="/settings">Settings</NavLink></nav>
      </header>
      <main><Routes><Route path="/jobs" element={<JobsView profile={profile} setProfile={setProfile} />} /><Route path="/applications" element={<ApplicationsView profile={profile} />} /><Route path="/companies" element={<CompaniesView profile={profile} />} /><Route path="/resume" element={<LegacyView kind="resume" />} /><Route path="/settings" element={<LegacyView kind="settings" />} /><Route path="*" element={<Navigate to="/jobs" replace />} /></Routes></main>
    </div>
  );
}

export default function App() {
  return <HashRouter><Shell /></HashRouter>;
}
