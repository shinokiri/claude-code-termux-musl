// Explicitly partial native evidence; never enable global auto-analysis.
//@category REAResearch
import ghidra.app.script.GhidraScript;
import ghidra.app.cmd.disassemble.DisassembleCommand;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.symbol.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.io.*;
import java.util.*;
import com.google.gson.Gson;

public class ResolverCallsitesEvidence extends GhidraScript {
    private final Gson gson = new Gson();
    private PrintWriter out;
    private void emit(Map<String,Object> row) { out.println(gson.toJson(row)); out.flush(); }
    private Map<String,Object> row(String kind) {
        Map<String,Object> r=new LinkedHashMap<>(); r.put("kind",kind); return r;
    }
    @Override public void run() throws Exception {
        String[] args=getScriptArgs();
        if(args.length!=2)throw new IllegalArgumentException("seed TSV and output JSONL required");
        out=new PrintWriter(Files.newBufferedWriter(Path.of(args[1]),StandardCharsets.UTF_8));
        try {
            Map<String,Object> first=row("identity");
            first.put("profile","elf-direct-import-callsites-v1");
            first.put("analysis_scope","Saved ELF-loader project plus aligned direct-branch candidates; no global autoanalysis");
            first.put("full_function_analysis",false);
            first.put("program_sha256",currentProgram.getExecutableSHA256());
            first.put("language",currentProgram.getLanguageID().toString());
            first.put("compiler_spec",currentProgram.getCompilerSpec().getCompilerSpecID().toString());
            first.put("image_base",currentProgram.getImageBase().toString());
            first.put("initial_instruction_count",currentProgram.getListing().getNumInstructions());
            emit(first);
            int total=0,count=0;
            for(String line:Files.readAllLines(Path.of(args[0]),StandardCharsets.UTF_8)) {
                if(line.isBlank())continue;
                String[] f=line.split("\t");int size=Integer.parseInt(f[2]);
                if(f.length!=4 || size<1 || size>4 || total+size>1024 || count>=128)
                    throw new IllegalArgumentException("bounded seed budget exceeded");
                Address start=toAddr(f[1]);AddressSet range=new AddressSet(start,start.add(size-1));
                DisassembleCommand command=new DisassembleCommand(start,range,true);
                command.enableCodeAnalysis(false);
                boolean ok=command.applyTo(currentProgram,monitor);
                Map<String,Object> x=row("bounded_disassembly");
                x.put("name",f[0]);x.put("seed_kind",f[3]);x.put("start",start.toString());
                x.put("requested_bytes",size);x.put("ok",ok);x.put("status",command.getStatusMsg());
                x.put("complete_function",false);
                List<Map<String,Object>> instructions=new ArrayList<>();
                InstructionIterator ii=currentProgram.getListing().getInstructions(range,true);
                while(ii.hasNext()) {
                    Instruction ins=ii.next();Map<String,Object> inst=new LinkedHashMap<>();
                    inst.put("address",ins.getAddress().toString());inst.put("text",ins.toString());
                    inst.put("length",ins.getLength());inst.put("flow_type",ins.getFlowType().toString());
                    inst.put("flows",Arrays.stream(ins.getFlows()).map(Object::toString).toList());
                    StringBuilder hex=new StringBuilder();for(byte b:ins.getBytes())hex.append(String.format("%02x",b&255));
                    inst.put("bytes",hex.toString());instructions.add(inst);
                }
                x.put("instructions",instructions);emit(x);total+=size;count++;
            }
            Map<String,Object> last=row("completed");last.put("seed_count",count);
            last.put("requested_disassembly_bytes",total);last.put("full_function_analysis",false);
            last.put("limitations",List.of("External symbols are loader observations, not proven runtime calls.",
                "PLT thunks do not identify full Bun or musl implementations.",
                "No global callers, complete functions, cross-language dataflow or privacy completeness claim."));
            emit(last);println("BOUNDED_NATIVE_EVIDENCE_COMPLETE seeds="+count+" bytes="+total);
        } finally { out.close(); }
    }
}
