import java.io.*;
import java.lang.instrument.Instrumentation;
import java.lang.reflect.Field;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.*;
import com.google.gson.*;
import ghidra.app.plugin.core.analysis.AutoAnalysisManager;
import ghidra.framework.data.DomainObjectAdapterDB;
import ghidra.program.model.address.*;
import ghidra.program.model.listing.*;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import ghidra.util.task.TaskMonitor;

/** Analyst-owned, one-shot export; no retransformation or target-code execution. */
public final class CheckpointAgent {
    private static final Gson JSON = new GsonBuilder().setPrettyPrinting().create();
    private static void write(Path path, Object value) throws Exception {
        Path tmp=path.resolveSibling(path.getFileName()+".tmp");
        Files.writeString(tmp,JSON.toJson(value)+"\n");
        Files.move(tmp,path,StandardCopyOption.REPLACE_EXISTING,StandardCopyOption.ATOMIC_MOVE);
    }
    private static String hex(byte[] bytes) { return HexFormat.of().formatHex(bytes); }
    private static void update(MessageDigest digest,String text) {digest.update((text+"\n").getBytes(StandardCharsets.UTF_8));}
    public static Map<String,Object> snapshot(Program p,JsonArray addresses) throws Exception {
        Map<String,Object> out=new LinkedHashMap<>();
        out.put("program_name",p.getName());
        out.put("source_sha256",p.getExecutableSHA256());
        out.put("language",p.getLanguageID().toString());
        out.put("compiler",p.getCompilerSpec().getCompilerSpecID().toString());
        out.put("function_count",p.getFunctionManager().getFunctionCount());
        out.put("instruction_count",p.getListing().getNumInstructions());
        out.put("defined_data_count",p.getListing().getNumDefinedData());
        MessageDigest funcs=MessageDigest.getInstance("SHA-256");
        int internal=0; FunctionIterator it=p.getFunctionManager().getFunctions(true);
        while(it.hasNext()) {
            Function f=it.next();internal++;
            update(funcs,f.getEntryPoint()+"\t"+f.getName()+"\t"+f.isThunk());
            AddressRangeIterator ranges=f.getBody().getAddressRanges();
            while(ranges.hasNext()) {AddressRange r=ranges.next();update(funcs,r.getMinAddress()+"-"+r.getMaxAddress());}
        }
        out.put("internal_function_count",internal);
        out.put("function_names_and_ranges_sha256",hex(funcs.digest()));
        List<Object> blocks=new ArrayList<>();
        for(MemoryBlock b:p.getMemory().getBlocks()) {
            Map<String,Object> v=new LinkedHashMap<>();
            v.put("name",b.getName());v.put("start",b.getStart().toString());v.put("size",b.getSize());v.put("initialized",b.isInitialized());
            if(b.getName().equals(".text")) {
                MessageDigest md=MessageDigest.getInstance("SHA-256");byte[] buffer=new byte[65536];long offset=0;
                while(offset<b.getSize()) {int n=(int)Math.min(buffer.length,b.getSize()-offset);int got=p.getMemory().getBytes(b.getStart().add(offset),buffer,0,n);if(got!=n)throw new IOException("Short memory read");md.update(buffer,0,n);offset+=n;}
                v.put("sha256",hex(md.digest()));
            }
            blocks.add(v);
        }
        out.put("memory_blocks",blocks);
        List<Object> probes=new ArrayList<>();
        for(JsonElement element:addresses) {
            String s=element.getAsString();Address a=p.getAddressFactory().getDefaultAddressSpace().getAddress(Long.parseUnsignedLong(s.replace("0x",""),16));
            Map<String,Object> v=new LinkedHashMap<>();v.put("address",s);
            byte[] b=new byte[4];if(p.getMemory().getBytes(a,b)!=4)throw new IOException("Short probe read");v.put("bytes",hex(b));
            Instruction instruction=p.getListing().getInstructionAt(a);v.put("instruction",instruction==null?null:instruction.toString());
            Function f=p.getFunctionManager().getFunctionContaining(a);v.put("function_entry",f==null?null:f.getEntryPoint().toString());v.put("function_name",f==null?null:f.getName());
            v.put("eol_comment",p.getListing().getComment(CodeUnit.EOL_COMMENT,a));
            List<String> refs=new ArrayList<>();for(Reference ref:p.getReferenceManager().getReferencesFrom(a))refs.add(ref.getToAddress()+"/"+ref.getReferenceType()+"/"+ref.getOperandIndex());Collections.sort(refs);v.put("references_from",refs);probes.add(v);
        }
        out.put("probes",probes);return out;
    }
    public static void agentmain(String argument,Instrumentation instrumentation) throws Exception {
        Path configPath=Path.of(argument);JsonObject config=JsonParser.parseString(Files.readString(configPath)).getAsJsonObject();
        Path directory=configPath.getParent();Path report=directory.resolve(config.get("receipt").getAsString());
        if(Files.exists(report))throw new IOException("Receipt already exists");
        Thread worker=new Thread(()->{
            Map<String,Object> receipt=new LinkedHashMap<>();Object consumer=new Object();Program program=null;
            try {
                receipt.put("started_utc",Instant.now().toString());receipt.put("pid",ProcessHandle.current().pid());receipt.put("class_redefinition",false);receipt.put("target_code_executed",false);
                Field field=AutoAnalysisManager.class.getDeclaredField("managerMap");field.setAccessible(true);
                List<Program> matching=new ArrayList<>();
                synchronized(AutoAnalysisManager.class) {
                    Map<?,?> managers=(Map<?,?>)field.get(null);
                    for(Object key:managers.keySet()) {Program p=(Program)key;if(config.get("sha256").getAsString().equals(p.getExecutableSHA256()))matching.add(p);}
                    if(matching.size()!=1)throw new IllegalStateException("Expected exactly one matching Program, got "+matching.size());
                    program=matching.get(0);program.addConsumer(consumer);
                }
                DomainObjectAdapterDB database=(DomainObjectAdapterDB)program;
                receipt.put("transaction_open",database.getCurrentTransactionInfo()!=null);
                if(database.getCurrentTransactionInfo()!=null)throw new IllegalStateException("Active transaction; refusing to force a save");
                receipt.put("state","snapshotting");write(report,receipt);
                Map<String,Object> before=snapshot(program,config.getAsJsonArray("addresses"));receipt.put("before",before);receipt.put("state","packing");write(report,receipt);
                Path output=directory.resolve(config.get("output").getAsString());Path partial=output.resolveSibling(output.getFileName()+".partial");
                if(Files.exists(output)||Files.exists(partial))throw new IOException("Output already exists");
                if(directory.toFile().getUsableSpace()<2L*1024*1024*1024)throw new IOException("Insufficient safety reserve");
                database.saveToPackedFile(partial.toFile(),TaskMonitor.DUMMY);
                try(FileOutputStream stream=new FileOutputStream(partial.toFile(),true)) {stream.getFD().sync();}
                Files.move(partial,output,StandardCopyOption.ATOMIC_MOVE);
                receipt.put("output",output.getFileName().toString());receipt.put("output_bytes",Files.size(output));
                receipt.put("state","checking-live-program");write(report,receipt);
                Map<String,Object> after=snapshot(program,config.getAsJsonArray("addresses"));receipt.put("after",after);receipt.put("live_observations_unchanged",before.equals(after));
                if(!before.equals(after))throw new IllegalStateException("Live snapshot changed during packing");
                receipt.put("packing_api","DomainObjectAdapterDB.saveToPackedFile");receipt.put("undo_history_may_be_pruned_by_packing_api",true);receipt.put("state","complete");receipt.put("finished_utc",Instant.now().toString());write(report,receipt);
            } catch(Throwable failure) {
                receipt.put("state","error");StringWriter s=new StringWriter();failure.printStackTrace(new PrintWriter(s));receipt.put("error",s.toString());try {write(report,receipt);}catch(Exception ignored){}
            } finally {if(program!=null)program.release(consumer);}
        },"authorized-program-checkpoint");worker.setDaemon(true);worker.start();
    }
}
