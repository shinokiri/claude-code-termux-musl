import ghidra.app.script.GhidraScript;
import ghidra.app.plugin.core.analysis.AutoAnalysisManager;
import ghidra.program.model.address.Address;
import java.nio.file.*;
public class CheckpointFixture extends GhidraScript {
    public void run() throws Exception {
        Path directory=Path.of(getScriptArgs()[0]);Address entry=toAddr(0x11000);
        int tx=currentProgram.startTransaction("checkpoint fixture metadata");
        try {disassemble(entry);createFunction(entry,"checkpoint_fixture_entry");setEOLComment(entry,"checkpoint fixture retained annotation");}
        finally {currentProgram.endTransaction(tx,true);}
        AutoAnalysisManager.getAnalysisManager(currentProgram);
        // Match ReaGhidraBridge.run(): commit GhidraScript's outer transaction
        // before the long-lived query loop starts.
        end(true);
        Files.writeString(directory.resolve("fixture-ready.txt"),ProcessHandle.current().pid()+"\n"+currentProgram.getExecutableSHA256()+"\n");
        while(!Files.exists(directory.resolve("fixture-release")))Thread.sleep(100);
        start();
        try {setEOLComment(toAddr(0x11004),"checkpoint post-save transaction works");}
        finally {end(true);}
        Files.writeString(directory.resolve("fixture-resumed.txt"),getEOLComment(toAddr(0x11004))+"\n");
    }
}
