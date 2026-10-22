import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.Collections;

/** Run independent assertions with KATch's unchanged parser and decision procedure. */
public final class KatchBatch {
    public static void main(String[] args) throws Throwable {
        nkpl.Runner$ runner = nkpl.Runner$.MODULE$;
        // Parse the network once and retain KATch's expression caches for the batch.
        scala.collection.immutable.Map<String, scala.util.Either<nkpl.NK, Object>> env =
            runner.runFile(scala.collection.immutable.Map$.MODULE$.empty(), args[0]);
        ArrayList<Path> queries = new ArrayList<>();
        try (DirectoryStream<Path> files =
                Files.newDirectoryStream(Paths.get(args[1]), "query-*.nkpl")) {
            for (Path file : files) queries.add(file);
        }
        Collections.sort(queries);
        for (Path query : queries) {
            try {
                runner.runFile(env, query.toString());
            } catch (Throwable failure) {
                // Only a normal false assertion permits the next independent query.
                // Parser errors, resource failures and other exceptions abort the batch.
                String marker = "\u001b[31m!!! Check failed in " + query + ":";
                if (failure.getClass() != Throwable.class
                        || failure.getMessage() == null
                        || !failure.getMessage().startsWith(marker)) throw failure;
                System.out.print(failure.getMessage());
            }
        }
    }
}
