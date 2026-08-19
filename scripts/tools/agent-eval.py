import json,os,shutil,subprocess,sys,tempfile,time,urllib.request
sys.path.insert(0,"/root")
from agent_tasks import TASKS

MODEL=sys.argv[1]; KEY=sys.argv[2]
REPS=int(sys.argv[3]) if len(sys.argv)>3 else 1
MAXTURNS=int(os.environ.get("MAXTURNS","18"))
URL="http://192.168.6.153:8000/v1/chat/completions"
OPTS={"qwen3.8-think":{"temperature":1.0,"top_p":0.95,"top_k":20,"min_p":0.0},
      "rag-qwen3.8":  {"temperature":0.7,"top_p":0.8,"top_k":20,"presence_penalty":1.5},
      "qwen3-coder":  {"temperature":0.15,"top_p":0.95}}
opts=OPTS.get(MODEL,{})

TOOLS=[
 {"type":"function","function":{"name":"list_files","description":"List files in the repo.",
  "parameters":{"type":"object","properties":{},"required":[]}}},
 {"type":"function","function":{"name":"read_file","description":"Read a file's full contents.",
  "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
 {"type":"function","function":{"name":"write_file","description":"Write (overwrite) a file with new contents.",
  "parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}},
 {"type":"function","function":{"name":"run_tests","description":"Run the repo's test suite and return output.",
  "parameters":{"type":"object","properties":{},"required":[]}}},
 {"type":"function","function":{"name":"finish","description":"Call when the task is complete and tests pass.",
  "parameters":{"type":"object","properties":{"summary":{"type":"string"}},"required":[]}}},
]

def run(cmd,cwd,timeout=90):
    try:
        r=subprocess.run(cmd,shell=True,cwd=cwd,capture_output=True,text=True,timeout=timeout)
        return (r.stdout+r.stderr)[-2500:], r.returncode
    except subprocess.TimeoutExpired:
        return "TIMEOUT", 1

def exec_tool(name,args,repo,verify):
    try:
        if name=="list_files":
            return "\n".join(sorted(os.listdir(repo))) or "(empty)"
        if name=="read_file":
            p=os.path.join(repo,os.path.basename(args.get("path","")))
            if not os.path.isfile(p): return f"ERROR: no such file: {args.get('path')}"
            return open(p).read()
        if name=="write_file":
            p=os.path.join(repo,os.path.basename(args.get("path","")))
            open(p,"w").write(args.get("content",""))
            return f"wrote {args.get('path')} ({len(args.get('content',''))} bytes)"
        if name=="run_tests":
            out,rc=run(verify,repo); return f"exit={rc}\n{out}"
        if name=="finish":
            return "acknowledged"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
    return "ERROR: unknown tool"

def call(messages):
    body={"model":MODEL,"messages":messages,"tools":TOOLS,"max_tokens":3000,**opts}
    req=urllib.request.Request(URL,data=json.dumps(body).encode(),
        headers={"Content-Type":"application/json","Authorization":f"Bearer {KEY}"})
    return json.load(urllib.request.urlopen(req,timeout=900))

def play(task,rep):
    repo=tempfile.mkdtemp(prefix=f"ag_{task['name']}_")
    for fn,body in task["files"].items(): open(os.path.join(repo,fn),"w").write(body)
    msgs=[{"role":"system","content":
           "You are a coding agent working in a small repo. Use the provided tools to inspect and "
           "modify files, and run the tests. Keep going until the tests pass, then call finish. "
           "Always verify with run_tests before calling finish."},
          {"role":"user","content":task["goal"]}]
    turns=0; tool_calls=0; errs=0; t0=time.time()
    while turns<MAXTURNS:
        turns+=1
        try: d=call(msgs)
        except Exception as e:
            return dict(ok=False,why=f"api_error:{type(e).__name__}",turns=turns,tools=tool_calls,errs=errs,secs=time.time()-t0,repo=repo)
        ch=d["choices"][0]; m=ch["message"]; tcs=m.get("tool_calls") or []
        am={"role":"assistant","content":m.get("content") or ""}
        if tcs: am["tool_calls"]=tcs
        msgs.append(am)
        if not tcs:
            msgs.append({"role":"user","content":"Continue using the tools. Run the tests and fix any failures, then call finish."})
            continue
        done=False
        for tc in tcs:
            fn=tc["function"]["name"]; tool_calls+=1
            try: args=json.loads(tc["function"].get("arguments") or "{}")
            except Exception: args={}
            res=exec_tool(fn,args,repo,task["verify"])
            if isinstance(res,str) and res.startswith("ERROR"): errs+=1
            msgs.append({"role":"tool","tool_call_id":tc.get("id",""),"name":fn,"content":str(res)[:4000]})
            if fn=="finish": done=True
        if done: break
    out,rc=run(task["verify"],repo)
    ok = rc==0
    shutil.rmtree(repo,ignore_errors=True)
    return dict(ok=ok,why=("pass" if ok else out.strip().splitlines()[-1][:80] if out.strip() else "fail"),
                turns=turns,tools=tool_calls,errs=errs,secs=time.time()-t0)

print(f"===== AGENTIC EVAL: {MODEL} (max {MAXTURNS} turns) =====",flush=True)
res=[]
for rep in range(REPS):
    for t in TASKS:
        r=play(t,rep); res.append((t["name"],r))
        print(f"  {t['name']:22} rep{rep}: {'PASS' if r['ok'] else 'FAIL'} "
              f"({r['why']}) turns={r['turns']} tools={r['tools']} tool_errs={r['errs']} {r['secs']:.0f}s",flush=True)
n=sum(1 for _,r in res if r["ok"])
print(f"  --> {n}/{len(res)}  mean turns {sum(r['turns'] for _,r in res)/len(res):.1f}  "
      f"mean tools {sum(r['tools'] for _,r in res)/len(res):.1f}  "
      f"mean {sum(r['secs'] for _,r in res)/len(res):.0f}s",flush=True)
json.dump([(n_,r) for n_,r in res],open(f"/root/agent_{MODEL}.json","w"))
