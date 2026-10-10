import fs from 'node:fs';
import assert from 'node:assert/strict';
import {spawn, spawnSync, execFile} from 'node:child_process';

const run = process.env.EXEC_TEST_RUN;
const preload = import.meta.path;
const binary = process.execPath;
const prefix = process.env.EXEC_TEST_PREFIX;
const tokens = ['argument with spaces', '', '雪', '--key=one two'];
const clean = extra => ({BUN_OPTIONS: '--preload ' + preload, EXEC_TEST_RUN: run,
  EXEC_TEST_PREFIX: prefix, PATH: process.env.PATH, ...extra});
const libraryProbe = "import os,ctypes; assert os.environ['CUSTOM_PRELOAD_LOADED']=='1'; assert ctypes.CDLL('libexec-env-test.so').fixture_value()==73; print('CUSTOM_LOADER_OK')";
if (process.env.EXEC_TEST_LOADER_CHILD) {
  assert.equal(process.env.LD_PRELOAD,undefined); assert.equal(process.env.LD_LIBRARY_PATH,undefined);
  const c=spawnSync(prefix+'/bin/python3',['-c',libraryProbe],{env:{...process.env}});
  assert.equal(c.status,0,c.stderr.toString());
  const empty=spawnSync(prefix+'/bin/python3',['-c',"import os; assert 'CUSTOM_PRELOAD_LOADED' not in os.environ; assert 'LD_LIBRARY_PATH' not in os.environ"],{env:{}});
  assert.equal(empty.status,0,empty.stderr.toString());
  process.stdout.write(c.stdout); process.exit(0);
}
const loaded = () => fs.readFileSync('/proc/self/maps','utf8').includes('/libtermux-musl-compat.so');
if (process.env.EXEC_TEST_CHILD) {
  const depth = Number(process.env.EXEC_TEST_CHILD);
  let child;
  if (depth > 1) {
    const result = spawnSync(binary, ['--version', ...tokens], {env: clean({EXEC_TEST_CHILD:String(depth-1)})});
    assert.equal(result.status, 0, result.stderr.toString());
    child = JSON.parse(result.stdout);
    assert.equal(child.pid, result.pid);
  }
  console.log(JSON.stringify({pid:process.pid, args:process.argv.slice(-tokens.length),
    loaded:loaded(), preloadAbsent:!process.env.LD_PRELOAD, child}));
  process.exit(0);
}

async function invoke(api, target, args, options) {
  if (api === 'bun-sync') {
    const c = Bun.spawnSync([target,...args], {...options, stdout:'pipe', stderr:'pipe'});
    return {code:c.exitCode, out:c.stdout.toString(), err:c.stderr.toString(), pid:c.pid};
  }
  if (api === 'node-sync') {
    const c = spawnSync(target,args,{...options, timeout:10000});
    return {code:c.status, out:c.stdout?.toString(), err:c.stderr?.toString(), pid:c.pid, error:c.error};
  }
  if (api === 'bun') {
    const c = Bun.spawn([target,...args],{...options, stdout:'pipe',stderr:'pipe'});
    const [out,err,code] = await Promise.all([new Response(c.stdout).text(),new Response(c.stderr).text(),c.exited]);
    return {code,out,err,pid:c.pid};
  }
  if (api === 'exec-file') return await new Promise(resolve => {
    const c=execFile(target,args,{...options,timeout:10000},(error,out,err)=>resolve({code:error?.code??0,out,err,pid:c.pid}));
  });
  return await new Promise((resolve,reject)=>{
    const c=spawn(target,args,{...options,stdio:['ignore','pipe','pipe']});
    let out='',err='';
    c.stdout.on('data',v=>out+=v); c.stderr.on('data',v=>err+=v);
    c.on('error',reject); c.on('close',code=>resolve({code,out,err,pid:c.pid}));
  });
}

const cases = [
  ['script',run+'/via-env',[],{env:clean({})}],
  ['relative-script','./via-env',[],{env:clean({}),cwd:run}],
  ['script-symlink',run+'/script-link',[],{env:clean({})}],
  ['env-split',run+'/via-env-split',[],{env:clean({})}],
  ['path-search','via-env',[],{env:{...clean({}),PATH:run+':'+process.env.PATH}}],
  ['absolute-alias','/bin/bash',['-c','printf SCRIPT_OK'],{env:{}}],
  ['bash-script',prefix+'/bin/bash',['-c','exec "$1"','boundary',run+'/via-env'],{env:clean({})}],
  ['self-three-generations',binary,['--version',...tokens],{env:clean({EXEC_TEST_CHILD:'3'})}],
  ['bash-self-three-generations',prefix+'/bin/bash',['-c','exec "$@"','boundary',binary,'--version',...tokens],{env:clean({EXEC_TEST_CHILD:'3'})}],
];
const results=[];
for (const [name,target,args,options] of cases) for (const api of ['bun','bun-sync','node','node-sync','exec-file']) {
  const result=await invoke(api,target,args,options);
  assert.equal(result.code,0,`${name}/${api}: ${JSON.stringify(result)}`);
  if (name.includes('self-three')) {
    let child=JSON.parse(result.out);
    assert.equal(child.pid,result.pid);
    for (let depth=0;depth<3;depth++,child=child.child) {
      assert.equal(child.loaded,true); assert.equal(child.preloadAbsent,true);
      assert.deepEqual(child.args,tokens);
    }
  } else assert.match(result.out,/SCRIPT_OK/,`${name}/${api}`);
  results.push({name,api,passed:true});
}
for (const api of ['execve','execv','execvp','execvpe','execl','execlp','execle','fexecve']) {
  const viaPath = ['execvp','execvpe','execlp'].includes(api);
  const result = spawnSync(run+'/exec-driver', [api,viaPath ? 'claude' : binary],
    {env:clean({EXEC_TEST_CHILD:'1',PATH:binary.slice(0,binary.lastIndexOf('/'))+':'+process.env.PATH})});
  assert.equal(result.status,0,api+': '+result.stderr.toString());
  const child = JSON.parse(result.stdout);
  assert.equal(child.pid,result.pid); assert.equal(child.loaded,true); assert.equal(child.preloadAbsent,true);
  assert.deepEqual(child.args,tokens); results.push({name:'bionic-libc',api,passed:true});
}
const customEnv = {LD_PRELOAD:run+'/custom-preload.so',LD_LIBRARY_PATH:run+'/libs'};
const directCustom=spawnSync(prefix+'/bin/python3',['-c',libraryProbe],{env:{...clean({}),...customEnv}});
assert.equal(directCustom.status,0,directCustom.stderr.toString()); assert.match(directCustom.stdout.toString(),/CUSTOM_LOADER_OK/);
// Modify loader variables after Bash has already loaded our library, then cross
// Bionic -> musl -> Bionic. The latest values must survive the round trip.
const customRoundTrip=spawnSync(prefix+'/bin/bash',['-c',
  'export LD_PRELOAD="$1" LD_LIBRARY_PATH="$2"; shift 2; exec "$@"','boundary',
  customEnv.LD_PRELOAD,customEnv.LD_LIBRARY_PATH,binary,'--version'],{env:clean({EXEC_TEST_LOADER_CHILD:'1'})});
assert.equal(customRoundTrip.status,0,customRoundTrip.stderr.toString()); assert.match(customRoundTrip.stdout.toString(),/CUSTOM_LOADER_OK/);
// Large argument vectors stay untouched for ELF executables.
const many=Array.from({length:2000},(_,i)=>'value '+i);
const args=spawnSync(prefix+'/bin/python3',['-c','import json,sys; print(json.dumps(sys.argv[1:]))',...many],{env:clean({})});
assert.equal(args.status,0,args.stderr.toString()); assert.deepEqual(JSON.parse(args.stdout),many);
// Actual cwd, stdin and additional file descriptor behavior survives exec.
const pipe=spawn(prefix+'/bin/python3',['-c',"import os,sys; print(os.getcwd()); print(sys.stdin.read()); os.write(3,b'FD_OK')"],
  {cwd:run,env:clean({}),stdio:['pipe','pipe','pipe','pipe']});
let pipeOut='',fdOut=''; pipe.stdout.on('data',v=>pipeOut+=v); pipe.stdio[3].on('data',v=>fdOut+=v);
pipe.stdin.end('STDIN_OK'); assert.equal(await new Promise(resolve=>pipe.on('close',resolve)),0);
assert.equal(pipeOut,run+'\nSTDIN_OK\n'); assert.equal(fdOut,'FD_OK');
const signal=spawn(prefix+'/bin/sleep',['30'],{env:{},stdio:'ignore'});
const stopped=new Promise(resolve=>signal.on('close',(code,sig)=>resolve({code,sig})));
signal.kill('SIGTERM'); assert.deepEqual(await stopped,{code:null,sig:'SIGTERM'});
for (const [target,code] of [[run+'/missing','ENOENT'],[run+'/not-executable','EACCES'],[run,'EACCES']]) {
  const result=spawnSync(target,[],{env:clean({})}); assert.equal(result.error?.code,code,JSON.stringify(result.error));
}
for (const name of ['ugrep','bfs']) {
  const result=spawnSync(binary,['--version'],{argv0:name,env:{PATH:process.env.PATH}});
  assert.equal(result.status,0,result.stderr.toString());
  assert.match(result.stdout.toString(),name==='ugrep'?/ugrep|ripgrep/i:/bfs/i);
}
fs.writeFileSync(process.env.EXEC_TEST_REPORT,JSON.stringify({cases:results,large_argv:2000,
  fd_and_cwd:true,signal:true,errors:true,builtin_argv0:true,custom_loader_environment:true,no_wrappers:true})+'\n');
process.exit(0);
