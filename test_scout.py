"""Offline tests: fake feeds in, check what the scanner keeps. Run: python test_scout.py"""
import json
import scout

GH_LIST = {"jobs": [
    {"id": 1, "title": "Learning Experience Designer", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1"},
    {"id": 2, "title": "Machine Learning Engineer", "absolute_url": "u2"},
    {"id": 3, "title": "Director, Learning & Development", "absolute_url": "u3"},
    {"id": 4, "title": "Instructional Designer", "absolute_url": "u4"},
    {"id": 5, "title": "L&D Specialist", "absolute_url": "u5"},
    {"id": 6, "title": "Sales Enablement Manager", "absolute_url": "u6"},
]}
GH_DETAIL = {
    1: {"location": {"name": "New York, NY"}, "first_published": "2026-11-09T10:00:00-05:00",
        "content": "&lt;p&gt;4+ years of experience in instructional design. Pay: $91,000 - $132,000 USD&lt;/p&gt;"},
    3: {"location": {"name": "New York, NY"}, "content": "x"},
    4: {"location": {"name": "Austin, TX"}, "content": "x"},
    5: {"location": {"name": "Remote - US"}, "content": "3 years experience",
        "pay_input_ranges": [{"min_cents": 8000000, "max_cents": 10000000}]},
    6: {"location": {"name": "New York City"}, "offices": [{"name": "NYC HQ"}],
        "content": "Requires 10+ years of sales enablement experience. $150,000 - $200,000"},
}
LEVER = [{"id": "a", "text": "Senior Learning Designer", "hostedUrl": "https://jobs.lever.co/x/a",
          "categories": {"location": "London", "allLocations": ["London"]}, "createdAt": 1794000000000,
          "descriptionPlain": "hi"},
         {"id": "b", "text": "AI Enablement Lead", "hostedUrl": "https://jobs.lever.co/x/b", "workplaceType": "remote",
          "categories": {"location": "United States"}, "createdAt": 1794000000000,
          "descriptionPlain": "5+ years of relevant experience", "salaryRange": {"min": 130000, "max": 160000, "interval": "per-year-salary"}}]
ASHBY = {"jobs": [{"id": "z", "title": "Talent Development Partner", "location": "New York", "isRemote": False,
                   "publishedAt": "2026-11-10T00:00:00Z", "jobUrl": "https://jobs.ashbyhq.com/y/z",
                   "descriptionPlain": "6-8 years of experience in L&D",
                   "compensation": {"summaryComponents": [{"compensationType": "Salary", "interval": "1 YEAR",
                                                           "minValue": 120000, "maxValue": 145000}]}}]}
WD_PAGE = {"jobPostings": [{"title": "Digital Enablement & AI Specialist", "externalPath": "/job/New-York/Spec_R9",
                            "locationsText": "New York City, NY", "postedOn": "Posted 3 Days Ago"}]}
WD_INFO = {"jobPostingInfo": {"location": "New York City, NY", "startDate": "2026-11-08",
                              "jobDescription": "<p>5+ years of experience in application training. $80,000 - $120,000</p>",
                              "externalUrl": "https://t.wd1.myworkdayjobs.com/s/job/New-York/Spec_R9"}}


def fake(url, body=None, timeout=0):
    if "greenhouse" in url:
        if url.endswith("/jobs"):
            return GH_LIST
        return GH_DETAIL[int(url.split("/jobs/")[1].split("?")[0])]
    if "lever" in url:
        return LEVER
    if "ashby" in url:
        return ASHBY
    if "myworkdayjobs" in url:
        return WD_PAGE if body is not None else WD_INFO
    if "broken" in url:
        raise RuntimeError("HTTP 404")
    raise RuntimeError("HTTP 404")


scout.http_json = fake
scout.today = lambda: scout.dt.date(2026, 11, 11)
companies = [
    {"name": "Acme", "ats": "greenhouse", "slug": "acme"},
    {"name": "Xco", "ats": "lever", "slug": "x"},
    {"name": "Yco", "ats": "ashby", "slug": "y"},
    {"name": "Tco", "ats": "workday", "tenant": "t", "wd": "wd1", "site": "s"},
    {"name": "Broken", "ats": "smartrecruiters", "slug": "broken"},
]
r = scout.run(companies, state={"jobs": {}}, write=False)
m = {j["title"]: j for j in r["matches"]}
s = {j["title"]: j["reason"] for j in r["skipped"]}
print(json.dumps({"matches": [(j["company"], j["title"], j["salary"], j["where"], j["years"], j["posted"], j["fresh"]) for j in r["matches"]],
                  "skipped": s, "problems": r["problems"]}, indent=1))

assert set(m) == {"Learning Experience Designer", "AI Enablement Lead", "Talent Development Partner",
                  "Digital Enablement & AI Specialist"}, set(m)
assert m["Learning Experience Designer"]["salary"] == "$91k–132k" and m["Learning Experience Designer"]["years"] == 4
assert m["AI Enablement Lead"]["where"] == "remote-us" and m["AI Enablement Lead"]["salary_max"] == 160000
assert m["Talent Development Partner"]["years"] == 6            # "6-8 years" counts as 6
assert m["Digital Enablement & AI Specialist"]["salary_max"] == 120000   # $80k–120k passes the rule
assert "Director" in s["Director, Learning & Development"]
assert "Pay too low" in s["L&D Specialist"]
assert "10+ years" in s["Sales Enablement Manager"]
assert "Instructional Designer" not in m and "Instructional Designer" not in s   # Austin: dropped quietly
assert "Machine Learning Engineer" not in m and "Senior Learning Designer" not in m
assert r["problems"] == [{"company": "Broken", "ats": "smartrecruiters", "error": "HTTP 404"}]
assert all(j["fresh"] for j in r["matches"] if j["posted"])
assert scout.parse_salary("range is $120K–$150K plus bonus") == (120000, 150000)
assert scout.parse_salary("$55 - $70 per hour") is None
assert scout.parse_salary("We raised $200M. Budget $5,000.") is None
assert scout.location_kind("Remote, Canada") is None and scout.location_kind("Remote") == "remote?"
assert scout.location_kind("San Francisco, CA; New York, NY") == "nyc"
assert scout.title_wanted("Senior L&D Specialist") and scout.title_wanted("Lead, Learning Architecture & AI Enablement")
assert not scout.title_wanted("Research Scientist, Reinforcement Learning")
feed = scout.feed_text(r)
assert "Acme | Learning Experience Designer" in feed
print("\nALL TESTS PASSED")
