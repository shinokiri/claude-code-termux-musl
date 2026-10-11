import com.sun.tools.attach.VirtualMachine;
public final class AttachCheckpoint {
    public static void main(String[] args) throws Exception {
        if(args.length!=3)throw new IllegalArgumentException("PID agent.jar config.json required");
        VirtualMachine vm=VirtualMachine.attach(args[0]);
        try {vm.loadAgent(args[1],args[2]);}finally {vm.detach();}
        System.out.println("Checkpoint worker requested for PID "+args[0]);
    }
}
