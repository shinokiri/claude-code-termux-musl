import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.*;
import ghidra.program.model.address.*;
import ghidra.program.model.mem.MemoryBlock;
import ghidra.program.model.symbol.Reference;
import com.google.gson.*;
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
public class VerifyCheckpoint extends GhidraScript {
    private void update(MessageDigest d,String text){d.update((text+"\n").getBytes(StandardCharsets.UTF_8));}
    public void run() throws Exception {
        String[] args=getScriptArgs();JsonObject config=JsonParser.parseString(Files.readString(Path.of(args[0]))).getAsJsonObject();
        Map<String,Object> out=new LinkedHashMap<>();out.put("program_name",currentProgram.getName());out.put("source_sha256",currentProgram.getExecutableSHA256());out.put("language",currentProgram.getLanguageID().toString());out.put("compiler",currentProgram.getCompilerSpec().getCompilerSpecID().toString());
        out.put("function_count",currentProgram.getFunctionManager().getFunctionCount());out.put("instruction_count",currentProgram.getListing().getNumInstructions());out.put("defined_data_count",currentProgram.getListing().getNumDefinedData());
        MessageDigest md=MessageDigest.getInstance("SHA-256");FunctionIterator it=currentProgram.getFunctionManager().getFunctions(true);int count=0;
        while(it.hasNext()) {Function f=it.next();count++;update(md,f.getEntryPoint()+"\t"+f.getName()+"\t"+f.isThunk());AddressRangeIterator ranges=f.getBody().getAddressRanges();while(ranges.hasNext()){AddressRange r=ranges.next();update(md,r.getMinAddress()+"-"+r.getMaxAddress());}}
        out.put("internal_function_count",count);out.put("function_names_and_ranges_sha256",HexFormat.of().formatHex(md.digest()));
        List<Object> blocks=new ArrayList<>();for(MemoryBlock b:currentProgram.getMemory().getBlocks()) {Map<String,Object> row=new LinkedHashMap<>();row.put("name",b.getName());row.put("start",b.getStart().toString());row.put("size",b.getSize());row.put("initialized",b.isInitialized());if(b.getName().equals(".text")){MessageDigest h=MessageDigest.getInstance("SHA-256");byte[] bytes=new byte[65536];long offset=0;while(offset<b.getSize()){int n=(int)Math.min(bytes.length,b.getSize()-offset);if(currentProgram.getMemory().getBytes(b.getStart().add(offset),bytes,0,n)!=n)throw new IllegalStateException("Short text read");h.update(bytes,0,n);offset+=n;}row.put("sha256",HexFormat.of().formatHex(h.digest()));}blocks.add(row);}out.put("memory_blocks",blocks);
        List<Object> probes=new ArrayList<>();for(JsonElement e:config.getAsJsonArray("addresses")){String s=e.getAsString();Address a=toAddr(Long.parseUnsignedLong(s.replace("0x",""),16));Map<String,Object> row=new LinkedHashMap<>();row.put("address",s);byte[] bytes=new byte[4];if(currentProgram.getMemory().getBytes(a,bytes)!=4)throw new IllegalStateException("Short probe read");row.put("bytes",HexFormat.of().formatHex(bytes));Instruction i=currentProgram.getListing().getInstructionAt(a);row.put("instruction",i==null?null:i.toString());Function f=currentProgram.getFunctionManager().getFunctionContaining(a);row.put("function_entry",f==null?null:f.getEntryPoint().toString());row.put("function_name",f==null?null:f.getName());row.put("eol_comment",currentProgram.getListing().getComment(CodeUnit.EOL_COMMENT,a));List<String> refs=new ArrayList<>();for(Reference ref:currentProgram.getReferenceManager().getReferencesFrom(a))refs.add(ref.getToAddress()+"/"+ref.getReferenceType()+"/"+ref.getOperandIndex());Collections.sort(refs);row.put("references_from",refs);probes.add(row);}out.put("probes",probes);
        Files.writeString(Path.of(args[1]),new GsonBuilder().setPrettyPrinting().create().toJson(out)+"\n");
    }
}
